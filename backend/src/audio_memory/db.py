from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from pathlib import Path
from urllib.parse import quote

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from audio_memory.models import Base
from audio_memory.config import PinnedDevelopmentRoot


# aiosqlite DEBUG records include raw execute parameters before SQLAlchemy can
# apply hide_parameters. Keep that driver boundary above content-bearing levels.
logging.getLogger("aiosqlite").setLevel(logging.WARNING)


class Database:
    def __init__(
        self,
        path: Path,
        *,
        write_boundary: PinnedDevelopmentRoot | None = None,
    ) -> None:
        self.path = path
        self.write_boundary = write_boundary
        database_url = f"sqlite+aiosqlite:///{path}"
        database_identity: tuple[int, int] | None = None
        if write_boundary is None:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            database_identity = write_boundary.ensure_regular_file(path)
            database_url = _sqlite_url(path, async_driver=True)
        self.engine: AsyncEngine = create_async_engine(
            database_url,
            connect_args={"check_same_thread": False},
            hide_parameters=True,
        )
        if write_boundary is not None:
            assert database_identity is not None

            def verify_development_database() -> None:
                write_boundary.verify_regular_file(path, database_identity)

            def verify_new_development_database(_connection, _record) -> None:
                verify_development_database()

            def verify_checked_out_development_database(
                _connection, record, _proxy
            ) -> None:
                try:
                    verify_development_database()
                except BaseException as exc:
                    record.invalidate(e=exc, soft=False)
                    raise

            def verify_development_transaction(connection) -> None:
                try:
                    verify_development_database()
                except BaseException as exc:
                    connection.invalidate(exc)
                    raise

            def verify_development_statement(
                connection, _cursor, _statement, _parameters, _context, _many
            ) -> None:
                try:
                    verify_development_database()
                except BaseException as exc:
                    connection.invalidate(exc)
                    raise

            self._verify_development_database = verify_development_database
            event.listen(
                self.engine.sync_engine,
                "connect",
                verify_new_development_database,
            )
            event.listen(
                self.engine.sync_engine,
                "checkout",
                verify_checked_out_development_database,
            )
            event.listen(
                self.engine.sync_engine,
                "begin",
                verify_development_transaction,
            )
            event.listen(
                self.engine.sync_engine,
                "before_cursor_execute",
                verify_development_statement,
            )
        else:
            self._verify_development_database = None
        event.listen(self.engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._verify_development_database is not None:
            self._verify_development_database()
        async with self._sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def run_migrations(
    database_path: Path,
    *,
    write_boundary: PinnedDevelopmentRoot | None = None,
) -> None:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    database_identity: tuple[int, int] | None = None
    if write_boundary is None:
        database_url = f"sqlite:///{database_path}"
    else:
        database_identity = write_boundary.ensure_regular_file(database_path)
        database_url = _sqlite_url(database_path, async_driver=False)
        config.attributes["connection_guard"] = lambda: (
            write_boundary.verify_regular_file(
                database_path, database_identity
            )
        )
    # Alembic stores options in ConfigParser, where percent-encoded URI paths
    # must escape percent signs even though SQLAlchemy ultimately receives one.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")
    if write_boundary is not None:
        write_boundary.verify_regular_file(database_path, database_identity)


def _sqlite_url(path: Path, *, async_driver: bool) -> str:
    driver = "sqlite+aiosqlite" if async_driver else "sqlite"
    encoded_path = quote(str(path), safe="/")
    return f"{driver}:///file:{encoded_path}?mode=rw&uri=true"
