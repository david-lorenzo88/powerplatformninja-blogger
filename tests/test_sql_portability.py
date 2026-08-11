"""Guards for the gap between SQLite and Azure SQL.

Every test in this suite runs on SQLite, and production runs on Azure SQL. That
asymmetry has now produced two bugs that were green all the way through CI and
failed on the first real request:

* ``.is_(True)`` compiles to ``feeds.enabled IS 1``. SQLite accepts it; SQL
  Server answers *Incorrect syntax near '1'* and the whole query dies.
* A ``DateTime(timezone=True)`` column reads back naive on SQLite and aware on
  Azure SQL, so comparing one to ``utcnow()`` raises only in production.
* SQLite ships with ``PRAGMA foreign_keys`` **off** — it stores the constraints
  and enforces none of them — so an orphan row is accepted in silence where
  Azure SQL rejects the INSERT outright.
* ``func.date(x)`` is SQLite's date truncation and **is not a function at all**
  on SQL Server, which answers *'date' is not a recognized built-in function
  name* and dies. This one reached `main`. Its mirror image is worse: the
  obvious "portable" fix, ``CAST(x AS DATE)``, is not an error on SQLite — the
  name carries no affinity keyword, so it yields the *year as an integer* and
  every day silently collapses into one bucket. Hence
  ``usage_store.day_bucket``, and the guard below.

Neither is catchable by running a query against SQLite, which is the only thing
the rest of the suite does. These tests compile statements against the SQL Server
dialect instead, and read the source for the pattern — so the guard covers code
nobody has written yet rather than only the queries listed here.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from sqlalchemy import Text, func, or_, select, true
from sqlalchemy.dialects import mssql

from ppn_blogger.server.db import Article, Base, Feed

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "ppn_blogger" / "server"

# `.is_(None)` is a genuine NULL test and compiles to IS NULL everywhere. It is
# only the boolean forms that are wrong.
BOOLEAN_IS = re.compile(r"\.is_\(\s*(True|False)\s*\)")


def test_no_module_uses_is_true_or_is_false() -> None:
    """The pattern, not just today's queries.

    A future `.is_(True)` would pass every other test in this suite and fail on
    the first real request, so it is caught here in the source rather than at
    runtime in Azure.
    """
    offenders: list[str] = []
    for path in sorted(SERVER_DIR.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if BOOLEAN_IS.search(line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")

    assert not offenders, (
        "`.is_(True)`/`.is_(False)` compiles to `IS 1` / `IS 0`, which SQL Server "
        "rejects with 'Incorrect syntax'. Use `== true()` / `== false()` from "
        "sqlalchemy instead — both render as `= 1` / `= 0` on every dialect:\n  "
        + "\n  ".join(offenders)
    )


# SQLite's own date/time functions. None of these exist on SQL Server, and none
# of them fail at *compile* time — the statement renders happily and dies on
# execution, so only a source-level guard catches them before a deploy.
SQLITE_ONLY_FUNCS = re.compile(r"\bfunc\.(date|time|datetime|julianday|strftime|unixepoch)\s*\(")

# The one legitimate use is inside a dialect branch. Marking it is deliberate:
# it makes writing a new one a conscious act rather than an accident.
GUARDED = "# sqlite-only:"


def test_no_module_calls_a_sqlite_only_date_function() -> None:
    """`func.date()` compiles on SQL Server and then fails on execution.

    That combination is why this is a grep and not a compile check — the
    statement is valid SQLAlchemy and invalid SQL, so nothing short of running
    it against a real server or reading the source will find it. It cost a
    merge to `main` before this guard existed.
    """
    offenders: list[str] = []
    for path in sorted(SERVER_DIR.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if SQLITE_ONLY_FUNCS.search(line) and GUARDED not in line:
                offenders.append(f"{path.name}:{number}: {line.strip()}")

    assert not offenders, (
        "SQLite-only date functions do not exist on SQL Server, and the "
        "statement compiles anyway — it fails only when it runs. Use "
        "`usage_store.day_bucket()`, which branches per dialect, or mark a "
        f"deliberate use with `{GUARDED}`:\n  " + "\n  ".join(offenders)
    )


def test_day_bucket_speaks_each_dialect() -> None:
    """Both branches, because both directions are wrong on the other backend.

    SQL Server rejects `date()` outright; SQLite accepts `CAST(x AS DATE)` and
    returns the year, which is the failure that would never be noticed.
    """
    from sqlalchemy.dialects import sqlite

    from ppn_blogger.server.db import RunUsage
    from ppn_blogger.server.usage_store import day_bucket

    on_sqlite = str(
        select(day_bucket(RunUsage.created_at, "sqlite")).compile(dialect=sqlite.dialect())
    )
    assert "date(" in on_sqlite.lower()

    on_mssql = str(
        select(day_bucket(RunUsage.created_at, "mssql")).compile(dialect=mssql.dialect())
    )
    assert "CAST(" in on_mssql and "AS DATE" in on_mssql
    assert "date(run_usage.created_at)" not in on_mssql.lower()


@pytest.mark.parametrize(
    "label,stmt",
    [
        ("ingest: enabled feeds", select(Feed).where(Feed.enabled == true())),
        (
            "ingest: realtime feeds",
            select(Feed).where(Feed.enabled == true(), Feed.realtime == true()),
        ),
        (
            "due feeds",
            select(Feed.id)
            .where(Feed.enabled == true())
            .where(or_(Feed.next_poll_at.is_(None), Feed.next_poll_at <= func.now())),
        ),
        (
            "counts: enabled",
            select(func.count()).select_from(Feed).where(Feed.enabled == true()),
        ),
        (
            "prune: uncited articles",
            select(Article.id).where(Article.used_in_issue_at.is_(None)),
        ),
    ],
)
def test_statements_compile_to_valid_sql_server(label: str, stmt) -> None:
    sql = str(stmt.compile(dialect=mssql.dialect()))
    assert " IS 1" not in sql and " IS 0" not in sql, f"{label} renders an invalid predicate:\n{sql}"


def test_boolean_predicates_render_the_same_on_both_backends() -> None:
    """`= 1` on SQL Server and on SQLite — so what the tests exercise is what runs."""
    from sqlalchemy.dialects import sqlite

    stmt = select(Feed.id).where(Feed.enabled == true())
    for dialect in (mssql.dialect(), sqlite.dialect()):
        assert "feeds.enabled = 1" in str(stmt.compile(dialect=dialect))


def test_no_indexed_column_is_text() -> None:
    """Text becomes NVARCHAR(max) on Azure SQL, which cannot carry an index at all.

    SQLite indexes it happily, so this only ever surfaces as a failed CREATE
    INDEX at first boot against a real database — after a deploy.
    """
    offenders = []
    for table in Base.metadata.tables.values():
        indexed: set[str] = {c.name for c in table.columns if c.index or c.unique}
        for index in table.indexes:
            indexed.update(c.name for c in index.columns)
        for name in sorted(indexed):
            column = table.columns[name]
            if isinstance(column.type, Text):
                offenders.append(f"{table.name}.{name}")

    assert not offenders, (
        "Indexed columns must be String(n), not Text — NVARCHAR(max) cannot be "
        f"indexed on Azure SQL: {', '.join(offenders)}"
    )


async def test_sqlite_enforces_foreign_keys(database_url) -> None:
    """The pragma must be on, on every connection.

    SQLite defaults it off, which made a third bug of this shape possible: test
    data referencing an article that did not exist passed locally and failed on
    SQL Server. The pragma is per *connection*, not per database, so it is set by
    a connect listener rather than once at startup — and this asserts it took,
    on whichever backend the suite is running.
    """
    from sqlalchemy import text

    from ppn_blogger.server import db

    await db.init_db()
    if not database_url.startswith("sqlite"):
        return  # SQL Server always enforces them

    async with db.session() as s:
        assert (await s.execute(text("PRAGMA foreign_keys"))).scalar() == 1


async def test_an_orphan_row_is_rejected(database_url) -> None:
    """The behaviour the pragma buys: the same INSERT fails on both backends."""
    import sqlalchemy.exc

    from ppn_blogger.server import db

    await db.init_db()
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        async with db.session() as s:
            s.add(db.NewsletterIssueItem(issue_id=999, newsletter_id=999, article_id=999))
            await s.commit()
