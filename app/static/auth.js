/* Cookies hold the session; tokens and account data stay in this page's memory. */
export class SessionChangedError extends Error {
  constructor() { super('Session changed'); this.name = 'SessionChangedError'; }
}
export const isSessionChange = error => error instanceof SessionChangedError;
export const accountCourseKey = userId => userId ? `zhixu.account.${encodeURIComponent(userId)}.course` : null;

export function createSessionClient({fetchImpl = (...args) => fetch(...args), onReset = () => {}, onSession = () => {}} = {}) {
  let session = {user:null, csrf_token:null}, epoch = 0;
  const pending = new Set();
  const invalidate = () => { epoch++; for (const controller of pending) controller.abort(); pending.clear(); };
  function apply(next, {force = false, reason = ''} = {}) {
    if (next?.user && (!next.user.id || !next.csrf_token)) throw new Error('登录响应无效，请重试。');
    const normalized = {user:next?.user || null, csrf_token:next?.csrf_token || null};
    const changed = normalized.user?.id !== session.user?.id;
    if (changed || force) { invalidate(); onReset(reason); }
    session = normalized;
    onSession(session, {changed:changed || force, reason});
    return session;
  }
  async function request(path, method = 'GET', data, {publicRequest = false} = {}) {
    if (!publicRequest && !session.user) throw new SessionChangedError();
    const started = epoch, controller = new AbortController();
    const headers = {};
    const options = {method, credentials:'same-origin', cache:'no-store', signal:controller.signal, headers};
    if (!publicRequest) {
      headers['X-Account-ID'] = session.user.id;
      if (!['GET','HEAD','OPTIONS'].includes(method.toUpperCase())) headers['X-CSRF-Token'] = session.csrf_token;
    }
    if (typeof FormData !== 'undefined' && data instanceof FormData) options.body = data;
    else if (data !== undefined) { headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(data); }
    pending.add(controller);
    try {
      const response = await fetchImpl('/api' + path, options);
      let result; try { result = await response.json(); } catch { result = {}; }
      if (started !== epoch) throw new SessionChangedError();
      if (!response.ok) {
        const code = result.detail?.code || result.code;
        if (!publicRequest && (response.status === 401 || code === 'account_changed' || code === 'csrf_invalid')) {
          apply(null, {force:true, reason:['account_changed','csrf_invalid'].includes(code)?'account_changed':'expired'});
          throw new SessionChangedError();
        }
        const known = {
          invalid_credentials:'邮箱或密码不正确。', email_in_use:'这个邮箱已注册，请直接登录。',
          registration_failed:'暂时无法创建账号，请检查信息或稍后重试。',
          invalid_email:'请输入有效的邮箱地址。', invalid_password:'密码需要 15–128 个字符。', invalid_name:'请输入 1–80 个字符的称呼。',
          invalid_recovery_token:'恢复链接无效或已过期，请重新从本机生成。',
          recovery_already_used:'此恢复链接已使用。',
          recovery_account_mismatch:'请登录恢复链接指定的邮箱账号。',
          recovery_conflict:'旧工作区的归属已变化，请重新从本机核对并生成恢复链接。',
          rate_limited:'尝试次数过多，请稍后再试。', csrf_invalid:'登录状态需要刷新，请刷新页面后重试。',
        };
        const message = known[code] || (typeof result.detail === 'string' ? result.detail : null);
        const error = new Error(message || '请求未完成（{status}），请检查输入后重试。');
        error.code = code;
        error.uiMessage = {key:error.message, values:{status:response.status}};
        throw error;
      }
      return result;
    } catch (error) {
      if (started !== epoch || error.name === 'AbortError') throw new SessionChangedError();
      throw error;
    } finally { pending.delete(controller); }
  }
  return {
    get session() { return session; },
    get epoch() { return epoch; },
    request,
    async restore() { return apply(await request('/auth/session','GET',undefined,{publicRequest:true})); },
    async authenticate(mode, fields) {
      if (!['login','register'].includes(mode)) throw new Error('Invalid authentication mode');
      const next = await request('/auth/' + mode,'POST',fields,{publicRequest:true});
      return apply(next, {force:true});
    },
    async logout() {
      // Clear visible data and abort old reads before the logout network round-trip.
      invalidate(); onReset('logout');
      try { await request('/auth/logout','POST'); return apply(null, {force:true}); }
      catch (error) { if (!isSessionChange(error)) onSession(session, {changed:true}); throw error; }
    },
    suspend(reason = '') { invalidate(); onReset(reason); },
  };
}
