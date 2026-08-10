"""One place that decides which database the suite runs against.

Locally that is a throwaway SQLite file per test — fast, no services, no
credentials, which is what makes the suite usable on a laptop. In CI it is a
real SQL Server, because SQLite is the more forgiving dialect in exactly the
places that matter and twice now has let code through that was green here and
broke on the first request in Azure (``IS 1``, and naive-vs-aware datetimes).

Set ``PPN_TEST_DATABASE_URL`` to point the whole suite at a real server:

    PPN_TEST_DATABASE_URL="mssql+aioodbc://sa:pw@localhost:1433/ppn_test?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=no" pytest -q

Isolation differs by necessity. Each SQLite test gets its own file and needs no
cleanup; a shared SQL Server does not, so the tables are dropped and recreated
between tests instead. That is the only behavioural difference, and it is here
rather than in the tests so no test has to know which backend it is on.
"""

from __future__ import annotations

import os

import pytest

SHARED_URL = os.environ.get("PPN_TEST_DATABASE_URL", "").strip()


def running_against_sql_server() -> bool:
    return bool(SHARED_URL) and not SHARED_URL.startswith("sqlite")


@pytest.fixture
async def database_url(tmp_path, monkeypatch):
    """Point the app at a clean database and hand back its URL.

    Every fixture that touches the database depends on this rather than setting
    PPN_DATABASE_URL itself, so switching backends is one environment variable.
    """
    url = SHARED_URL or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("PPN_DATABASE_URL", url)

    from ppn_blogger.server import db

    await db.reset_engine()

    if running_against_sql_server():
        # A shared server carries the previous test's rows; a per-test SQLite
        # file cannot. Drop before rather than after, so a crashed test still
        # leaves the next one a clean database.
        async with db.engine().begin() as conn:
            await conn.run_sync(db.Base.metadata.drop_all)
        await db.reset_engine()

    yield url

    await db.reset_engine()
