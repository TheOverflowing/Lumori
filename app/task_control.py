"""Host-owned task limits and cancellation, distinct from upstream failures."""


class LocalCallLimit(ValueError):
    """A rejected local reservation: no upstream request was made.

    Daily limits can recover without replacing a checkpoint. A frozen lifetime
    job budget cannot be reset by resume or by a UTC date change.
    """

    def __init__(self, message, *, scope):
        if scope not in ('daily', 'job'):
            raise ValueError('Unknown local call limit scope.')
        self.scope = scope
        super().__init__(message)

    @property
    def resource_limit(self):
        return {'resource': 'api_calls', 'scope': self.scope,
                'resumable': self.scope == 'daily'}


class JobCancelled(BaseException):
    """Stop at a safe boundary without triggering fallback or paid retries.

    This is intentionally not an Exception: best-effort retrieval and repair
    handlers must not swallow an explicit user cancellation.
    """
