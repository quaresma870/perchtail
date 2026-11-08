from logging.config import fileConfig

import app.db  # noqa: F401  ensures the sqlite parent directory exists
from alembic import context
from app.config import get_settings
from app.data_layout import migrate_legacy_layout
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config
# A pre-0.5.0 layout is moved first, so upgrading can never create an empty
# database next to the real one (app/data_layout.py).
migrate_legacy_layout()
config.set_main_option("sqlalchemy.url", get_settings().database_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Models register themselves on SQLModel.metadata on import; as models are
# added (starting with M1 — Data model), import them here so autogenerate
# picks them up.
target_metadata = SQLModel.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def _include_object(obj, name, type_, reflected, compare_to):
    """FTS5 tables (file_name_fts, see app/name_index_store.py) and their
    shadow tables are created in raw SQL, not from the models, so autogenerate
    and `alembic check` leave them alone."""
    return not (type_ == "table" and name is not None and name.startswith("file_name_fts"))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        include_object=_include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
