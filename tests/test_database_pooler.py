import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from context_agent.config import Settings
from context_agent.db import database_engine


def test_transaction_mode_preserves_connection_identity_and_options():
    original = "postgresql+asyncpg://postgres.example:fake%40password@aws-0-test.pooler.supabase.com:5432/postgres?ssl=require"
    settings = Settings(_env_file=None, database_url=original, database_pooler_mode="transaction")
    before, after = make_url(original), make_url(settings.database_url)
    assert after == before.set(port=6543)
    assert after.password == "fake@password"
    assert settings.database_pooler_mode == "transaction"
    assert Settings(_env_file=None, database_url=original).database_url == original


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///:memory:",
        "postgresql+asyncpg://test:fake@localhost:5432/test",
        "postgresql+asyncpg://test:fake@db.example.supabase.co:5432/test",
        "postgresql+asyncpg://test:fake@evilpooler.supabase.com:5432/test",
    ],
)
def test_pooler_mode_does_not_retarget_unrelated_database(url):
    with pytest.raises(ValidationError, match="Supabase asyncpg pooler"):
        Settings(_env_file=None, database_url=url, database_pooler_mode="transaction")


async def test_transaction_engine_avoids_retained_connections_and_statement_cache():
    engine = database_engine(
        "postgresql+asyncpg://test:fake@aws-0-test.pooler.supabase.com:6543/postgres"
    )
    assert isinstance(engine.pool, NullPool)
    # Inspect what SQLAlchemy actually passes to the DBAPI rather than opening
    # a connection to an external database in a unit test.
    captured = {}
    from sqlalchemy import event

    class StopConnect(Exception):
        pass

    def capture(dialect, record, args, kwargs):
        captured.update(kwargs)
        raise StopConnect()

    event.listen(engine.sync_engine, "do_connect", capture)
    try:
        with pytest.raises(StopConnect):
            async with engine.connect():
                pass
        assert captured["statement_cache_size"] == 0
        assert captured["prepared_statement_cache_size"] == 0
        names = [captured["prepared_statement_name_func"]() for _ in range(2)]
        assert names[0] != names[1]
    finally:
        await engine.dispose()
