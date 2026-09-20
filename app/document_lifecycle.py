"""Reversible deletion and retrieval opt-out; historical citations remain readable."""
from .store import now

# Missing state means enabled, preserving old databases and offline fixtures.
ACTIVE_SQL="NOT EXISTS (SELECT 1 FROM document_lifecycle dl WHERE dl.document_id=d.id AND (dl.enabled=0 OR dl.deleted_at IS NOT NULL))"


def state(store,did):
    row=store.one('SELECT enabled,deleted_at FROM document_lifecycle WHERE document_id=?',(did,))
    return {'enabled':bool(row['enabled']) if row else True,'deleted_at':row['deleted_at'] if row else None}


def set_enabled(store,did,enabled):
    store.execute('''INSERT INTO document_lifecycle(document_id,enabled) VALUES(?,?)
        ON CONFLICT(document_id) DO UPDATE SET enabled=excluded.enabled''',(did,int(enabled)))


def trash(store,did):
    store.execute('''INSERT INTO document_lifecycle(document_id,enabled,deleted_at) VALUES(?,0,?)
        ON CONFLICT(document_id) DO UPDATE SET enabled=0,deleted_at=COALESCE(document_lifecycle.deleted_at,excluded.deleted_at)''',(did,now()))


def restore(store,did):
    # Restore disabled: returning a document must not silently change the corpus.
    store.execute('UPDATE document_lifecycle SET deleted_at=NULL,enabled=0 WHERE document_id=? AND deleted_at IS NOT NULL',(did,))
