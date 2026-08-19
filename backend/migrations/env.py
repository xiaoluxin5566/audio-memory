from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from audio_memory.db import configure_sqlite_migration_connection
from audio_memory.models import Base


config = context.config
if config.config_file_name is not None:
    # Migrations run inside the already-configured web process.  The default
    # disables every logger not named by alembic.ini, including uvicorn and our
    # structured lifecycle loggers.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    event.listen(connectable, "connect", configure_sqlite_migration_connection)
    with connectable.connect() as connection:
        connection_guard = config.attributes.get("connection_guard")
        if connection_guard is not None:
            connection_guard()
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
