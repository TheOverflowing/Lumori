"""One editable quality rating per owned content version, with a retained audit trail."""
import json

from .store import Conflict, dumps, now, uid


def _current(db, content_id, version):
    row = db.execute('''SELECT e.*,c.updated_at FROM current_evaluations c
        JOIN evaluations e ON e.id=c.evaluation_id
        WHERE c.content_id=? AND c.version=?''', (content_id, version)).fetchone()
    if row:
        return dict(row)
    # Old repeated submissions remain intact; the latest one is the initial
    # current rating. No destructive migration or reinterpretation of scores.
    row = db.execute('''SELECT *,created_at AS updated_at FROM evaluations
        WHERE content_id=? AND version=? ORDER BY created_at DESC,rowid DESC LIMIT 1''',
        (content_id, version)).fetchone()
    return dict(row) if row else None


def _snapshot(row):
    if row is None:
        return None
    return {key: json.loads(row[key]) if key == 'metrics' else row[key]
            for key in ('id', 'version', 'metrics', 'created_at', 'updated_at')}


def current_evaluation(store, content_id, version):
    """Caller must first enforce content ownership."""
    with store.connect() as db:
        return _snapshot(_current(db, content_id, version))


def save_evaluation(store, content_id, version, metrics):
    """Validated metrics are serialized under the same lock as the version check."""
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        content = db.execute('SELECT version FROM contents WHERE id=?', (content_id,)).fetchone()
        if not content or content['version'] != version:
            raise Conflict('内容已更新，请刷新后重新评价。')
        previous = _current(db, content_id, version)
        created = previous is None
        stamp = now()
        if created:
            evaluation_id = uid()
            db.execute('INSERT INTO evaluations VALUES(?,?,?,?,?)',
                       (evaluation_id, content_id, version, dumps(metrics), stamp))
        else:
            evaluation_id = previous['id']
            if json.loads(previous['metrics']) != metrics:
                db.execute('INSERT INTO evaluation_history VALUES(?,?,?,?)',
                           (uid(), evaluation_id, previous['metrics'], previous['updated_at']))
                db.execute('UPDATE evaluations SET metrics=? WHERE id=?', (dumps(metrics), evaluation_id))
            else:
                stamp = previous['updated_at']
        db.execute('''INSERT INTO current_evaluations VALUES(?,?,?,?)
            ON CONFLICT(content_id,version) DO UPDATE SET
            evaluation_id=excluded.evaluation_id,updated_at=excluded.updated_at''',
            (content_id, version, evaluation_id, stamp))
        return {'id': evaluation_id, 'created': created,
                'evaluation': _snapshot(_current(db, content_id, version))}


# Export one current observation per content/version; archived duplicates and
# prior edits stay in SQLite for audit instead of inflating evaluation counts.
CURRENT_EXPORT_SQL = '''SELECT e.* FROM evaluations e JOIN contents c ON c.id=e.content_id
    JOIN course_owners o ON o.course_id=c.course_id WHERE o.user_id=? AND e.id=COALESCE(
        (SELECT evaluation_id FROM current_evaluations ce WHERE ce.content_id=e.content_id AND ce.version=e.version),
        (SELECT e2.id FROM evaluations e2 WHERE e2.content_id=e.content_id AND e2.version=e.version
         ORDER BY e2.created_at DESC,e2.rowid DESC LIMIT 1)) ORDER BY e.created_at'''
