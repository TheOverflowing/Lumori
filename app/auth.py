"""Local accounts and opaque, revocable browser sessions.

Passwords use the OWASP scrypt baseline (N=2**17, r=8, p=1). Only session
token digests are persisted. HTTP routes must additionally enforce same-origin
requests before accepting login/register and validate CSRF on authenticated writes.
"""
from dataclasses import dataclass, field
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
import unicodedata

from .store import now, uid


SCRYPT_N = 2 ** 17
SCRYPT_R = 8
SCRYPT_P = 1
_HASH_SLOTS = threading.BoundedSemaphore(2)
_TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9_-]{43}$')
_EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")


class AuthError(Exception):
    def __init__(self, status_code, code, message, *, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


def normalize_email(email):
    value = email.strip().lower() if isinstance(email, str) else ''
    local = value.split('@')[0]
    if (len(value) > 254 or not _EMAIL_PATTERN.fullmatch(value)
            or len(local) > 64 or local.startswith('.') or local.endswith('.') or '..' in local
            or any(len(label) > 63 for label in value.split('@')[-1].split('.'))):
        raise AuthError(400, 'invalid_email', '请输入有效的邮箱地址。')
    return value


def validate_password(password):
    if not isinstance(password, str) or not 15 <= len(password) <= 128:
        raise AuthError(400, 'invalid_password', '密码需要 15–128 个字符，可使用中文或短语。')
    try:
        password.encode('utf-8')
    except UnicodeEncodeError:
        raise AuthError(400, 'invalid_password', '密码包含无法保存的字符。') from None


def _derive(password, salt):
    with _HASH_SLOTS:
        return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=SCRYPT_N,
                              r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=256 * 1024 * 1024)


def hash_password(password):
    validate_password(password)
    salt = secrets.token_bytes(16)
    derived = _derive(password, salt)
    return f'scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}'


def verify_password(password, encoded):
    if not isinstance(password, str) or not 1 <= len(password) <= 128:
        return False
    try:
        password.encode('utf-8')
        name, n, r, p, salt, digest = encoded.split('$')
        # Do not let malformed database values choose arbitrary resource costs.
        if (name, n, r, p) != ('scrypt', str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)):
            return False
        salt, digest = bytes.fromhex(salt), bytes.fromhex(digest)
        if len(salt) != 16 or len(digest) != 32:
            return False
        return hmac.compare_digest(_derive(password, salt), digest)
    except (ValueError, TypeError, AttributeError, UnicodeEncodeError):
        return False


def _digest(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _csrf(token):
    # A domain-separated token bound to this session. The browser cannot read
    # the HttpOnly session cookie and obtains only this value from /auth/session.
    return hmac.new(token.encode('ascii'), b'fyp-csrf-v1', hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class AuthSession:
    user: dict
    token: str = field(repr=False)
    expires_at: int

    @property
    def csrf_token(self):
        return _csrf(self.token)

    def response(self):
        return {'user': self.user, 'csrf_token': self.csrf_token, 'expires_at': self.expires_at}


class AuthService:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', settings.auth_cookie_name)
                or settings.auth_session_hours < 1
                or settings.auth_throttle_window_seconds < 1
                or settings.auth_login_limit < 1 or settings.auth_register_limit < 1):
            raise ValueError('认证配置无效。')

    @staticmethod
    def _public_user(user):
        return {key: user[key] for key in ('id', 'email', 'display_name', 'created_at')}

    def _consume_attempt(self, action, email, client_ip):
        stamp = int(time.time())
        window = self.settings.auth_throttle_window_seconds
        buckets = [(f'{action}:ip:{client_ip}', self.settings.auth_register_limit
                    if action == 'register' else self.settings.auth_login_limit * 3)]
        if action == 'login':
            buckets.append((f'{action}:email:{email}', self.settings.auth_login_limit))
        # Hash arbitrary email/IP bucket labels to bound storage and avoid
        # retaining raw failed-login identifiers in the throttle table.
        buckets = [(hashlib.sha256(key.encode()).hexdigest(), limit) for key, limit in buckets]
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM auth_throttles WHERE window_start<=?', (stamp-window,))
            for key, limit in buckets:
                old = db.execute('SELECT attempts,window_start FROM auth_throttles WHERE bucket=?', (key,)).fetchone()
                if old and old['attempts'] >= limit:
                    raise AuthError(429, 'rate_limited', '尝试次数过多，请稍后再试。',
                                    retry_after=max(1, old['window_start'] + window - stamp))
            for key, _ in buckets:
                db.execute('''INSERT INTO auth_throttles VALUES(?,1,?) ON CONFLICT(bucket)
                    DO UPDATE SET attempts=auth_throttles.attempts+1''', (key, stamp))

    def _new_session(self, db, user):
        stamp = int(time.time())
        token = secrets.token_urlsafe(32)
        expires_at = stamp + self.settings.auth_session_hours * 3600
        db.execute('DELETE FROM auth_sessions WHERE expires_at<=?', (stamp,))
        db.execute('INSERT INTO auth_sessions VALUES(?,?,?,?)', (_digest(token), user['id'], stamp, expires_at))
        return AuthSession(self._public_user(user), token, expires_at)

    def register(self, email, password, display_name, client_ip='local'):
        self._consume_attempt('register', '', client_ip)
        email = normalize_email(email)
        validate_password(password)
        display_name = display_name.strip() if isinstance(display_name, str) else ''
        if (not 1 <= len(display_name) <= 80
                or any(unicodedata.category(char).startswith('C') for char in display_name)):
            raise AuthError(400, 'invalid_name', '显示名称需要 1–80 个可见字符。')
        user = dict(id=uid(), email=email, password_hash=hash_password(password),
                    display_name=display_name, created_at=now())
        try:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO users VALUES(:id,:email,:password_hash,:display_name,:created_at)', user)
                # No ownership migration is performed when a user registers.
                return self._new_session(db, user)
        except sqlite3.IntegrityError:
            raise AuthError(409, 'email_in_use', '这个邮箱已注册，请登录。') from None

    def login(self, email, password, client_ip='local'):
        email = normalize_email(email)
        self._consume_attempt('login', email, client_ip)
        user = self.store.one('SELECT * FROM users WHERE email=?', (email,))
        correct = verify_password(password, user['password_hash']) if user else False
        if not user and isinstance(password, str) and 1 <= len(password) <= 128:
            try:
                _derive(password, b'\x00' * 16)
            except UnicodeEncodeError:
                pass
        if not correct:
            raise AuthError(401, 'invalid_credentials', '邮箱或密码不正确。')
        with self.store.connect() as db:
            return self._new_session(db, user)

    def authenticate(self, token):
        if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
            return None
        row = self.store.one('''SELECT u.id,u.email,u.display_name,u.created_at,s.expires_at
            FROM auth_sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>?''', (_digest(token), int(time.time())))
        return AuthSession(self._public_user(row), token, row['expires_at']) if row else None

    def logout(self, token):
        if isinstance(token, str) and _TOKEN_PATTERN.fullmatch(token):
            self.store.execute('DELETE FROM auth_sessions WHERE token_hash=?', (_digest(token),))

    @staticmethod
    def check_csrf(session, submitted_token):
        return (isinstance(submitted_token, str) and re.fullmatch(r'[0-9a-f]{64}', submitted_token) is not None
                and hmac.compare_digest(session.csrf_token, submitted_token))

    def set_cookie(self, response, session):
        response.set_cookie(self.settings.auth_cookie_name, session.token,
                            max_age=max(0, session.expires_at-int(time.time())),
                            httponly=True, secure=self.settings.auth_cookie_secure,
                            samesite='lax', path='/')

    def clear_cookie(self, response):
        response.delete_cookie(self.settings.auth_cookie_name, path='/',
                               secure=self.settings.auth_cookie_secure,
                               httponly=True, samesite='lax')
