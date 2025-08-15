# backend/app/core/db.py
from contextlib import contextmanager
from psycopg import connect
from psycopg.rows import dict_row

try:
    from app.core.settings import settings
except Exception:
    class _S:
        PG_HOST = "127.0.0.1"
        PG_PORT = 5432
        PG_DB = "agentstack"
        PG_USER = "postgres"
        PG_PASSWORD = "postgres"
    settings = _S()

@contextmanager
def get_conn(*, cursor_factory=dict_row):
    with connect(
        host=getattr(settings, "PG_HOST", "127.0.0.1"),
        port=getattr(settings, "PG_PORT", 5432),
        dbname=getattr(settings, "PG_DB", "agentstack"),
        user=getattr(settings, "PG_USER", "postgres"),
        password=getattr(settings, "PG_PASSWORD", "postgres"),
        autocommit=False,
        row_factory=cursor_factory,
    ) as _cn:
        yield _cn
