from datetime import datetime
import inspect
import logging
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from typing import Callable, List, Optional, Union
import uuid

import pandas as pd
import sqlalchemy
from sqlalchemy import create_engine
from sqlalchemy import Engine
from sqlalchemy import inspect as sql_inspect

from trulens_eval.database import base as mod_db
from trulens_eval.database.base import LocalSQLite
from trulens_eval.database.exceptions import DatabaseVersionException
from trulens_eval.database.migrations import DbRevisions
from trulens_eval.database.migrations import upgrade_db

logger = logging.getLogger(__name__)


def for_all_methods(decorator, _except: Optional[List[str]] = None):
    """
    Applies decorator to all methods except classmethods, private methods and
    the ones specified with `_except`.
    """

    def decorate(cls):

        for attr_name, attr in cls.__dict__.items(
        ):  # does not include classmethods

            if not inspect.isfunction(attr):
                continue  # skips non-method attributes

            if attr_name.startswith("_"):
                continue  # skips private methods

            if _except is not None and attr_name in _except:
                continue

            logger.debug("Decorating %s", attr_name)
            setattr(cls, attr_name, decorator(attr))

        return cls

    return decorate


def run_before(callback: Callable):
    """
    Create decorator to run the callback before the function.
    """

    def decorator(func):

        def wrapper(*args, **kwargs):
            callback(*args, **kwargs)
            return func(*args, **kwargs)

        return wrapper

    return decorator


def is_legacy_sqlite(engine: Engine) -> bool:
    """
    Check if DB is an existing file-based SQLite created with the `LocalSQLite`
    implementation.

    Will also return True for an empty, brand-new SQLite file, but applying the
    legacy migrations on the empty database before the Alembic scripts will not
    hurt, so this should be a harmless false positive.

    Checking for the existence of the `meta` table is not safe because in
    trulens_eval 0.1.2 the meta table may not exist.
    """

    inspector = sql_inspect(engine)
    tables = list(inspector.get_table_names())

    if len(tables) == 0:
        # brand new db, not even initialized yet
        return False

    version_tables = [t for t in tables if t.endswith("alembic_version")]

    return len(version_tables) == 0

    #return DbRevisions.load(engine).current is None


def is_memory_sqlite(
    engine: Optional[Engine] = None,
    url: Optional[Union[sqlalchemy.engine.URL, str]] = None
) -> bool:
    """Check if DB is an in-memory SQLite instance.

    Either engine or url can be provided.
    """

    if isinstance(engine, Engine):
        url = engine.url

    elif isinstance(url, sqlalchemy.engine.URL):
        pass

    elif isinstance(url, str):
        url = sqlalchemy.engine.make_url(url)

    else:
        raise ValueError("Either engine or url must be provided")

    return (
        # The database type is SQLite
        url.drivername.startswith("sqlite")

        # The database storage is in memory
        and url.database == ":memory:"
    )


def check_db_revision(
    engine: Engine,
    prefix: str = mod_db.DEFAULT_DATABASE_PREFIX,
    prior_prefix: Optional[str] = None
):
    """
    Check if database schema is at the expected revision.

    Args:
        engine: SQLAlchemy engine to check.

        prefix: Prefix used for table names including alembic_version in the
            current code.

        prior_prefix: Table prefix used in the previous version of the
            database. Before this configuration was an option, the prefix was
            equivalent to "".
    """

    if not isinstance(prefix, str):
        raise ValueError("prefix must be a string")

    if prefix == prior_prefix:
        raise ValueError("prior_prefix and prefix canot be the same. Use None for prior_prefix if it is unknown.")

    ins = sqlalchemy.inspect(engine)
    tables = ins.get_table_names()

    # Get all tables we could have made for alembic version. Other apps might
    # also have made these though.
    version_tables = [t for t in tables if t.endswith("alembic_version")]

    if prior_prefix is not None:
        # Check if tables using the old/empty prefix exist.
        if prior_prefix + "alembic_version" in version_tables:
            raise DatabaseVersionException.reconfigured(prior_prefix=prior_prefix)
    else:
        # Check if the new/expected version table exists.

        if prefix + "alembic_version" not in version_tables:
            # If not, lets try to figure out the prior prefix.

            if len(version_tables) > 0:

                if len(version_tables) > 1:
                    # Cannot figure out prior prefix if there is more than one
                    # version table.
                    raise ValueError(
                        f"Found multiple alembic_version tables: {version_tables}. "
                        "Cannot determine prior prefix. "
                        "Please specify it using the `prior_prefix` argument."
                    )

                # Guess prior prefix as the single one with version table name.
                raise DatabaseVersionException.reconfigured(
                    prior_prefix=version_tables[0].replace("alembic_version", "")
                )

    if is_legacy_sqlite(engine):
        logger.info("Found legacy SQLite file: %s", engine.url)
        raise DatabaseVersionException.behind()

    revisions = DbRevisions.load(engine, prefix=prefix)

    if revisions.current is None:
        logger.debug("Creating database")
        upgrade_db(
            engine, revision="head", prefix=prefix
        )  # create automatically if it doesn't exist

    elif revisions.in_sync:
        logger.debug("Database schema is up to date: %s", revisions)

    elif revisions.behind:
        raise DatabaseVersionException.behind()

    elif revisions.ahead:
        raise DatabaseVersionException.ahead()

    else:
        raise NotImplementedError(
            f"Cannot handle database revisions: {revisions}"
        )


def migrate_legacy_sqlite(engine: Engine, prefix: str = "trulens_"):
    """
    Migrate legacy file-based SQLite to the latest Alembic revision:

    Migration plan:
        1. Make sure that original database is at the latest legacy schema.
        2. Create empty staging database at the first Alembic revision.
        3. Copy records from original database to staging.
        4. Migrate staging database to the latest Alembic revision.
        5. Replace original database file with the staging one.

    Assumptions:
        1. The latest legacy schema is not identical to the first Alembic
           revision, so it is not safe to apply the Alembic migration scripts
           directly (e.g.: TEXT fields used as primary key needed to be changed
           to VARCHAR due to limitations in MySQL).
        2. The latest legacy schema is similar enough to the first Alembic
           revision, and SQLite typing is lenient enough, so that the data
           exported from the original database can be loaded into the staging
           one.
    """

    # 1. Make sure that original database is at the latest legacy schema
    if not is_legacy_sqlite(engine):
        raise ValueError("Not a legacy SQLite database")

    original_file = Path(engine.url.database)
    saved_db_file = original_file.parent / f"{original_file.name}_saved_{uuid.uuid1()}"
    shutil.copy(original_file, saved_db_file)
    logger.info(
        "Saved original db file: `%s` to new file: `%s`", original_file, saved_db_file
    )
    logger.info("Handling legacy SQLite file: %s", original_file)
    logger.debug("Applying legacy migration scripts")
    LocalSQLite(filename=original_file).migrate_database()

    with TemporaryDirectory() as tmp:

        # 2. Create empty staging database at first Alembic revision
        stg_file = Path(tmp).joinpath("migration-staging.sqlite")
        logger.debug("Creating staging DB at %s", stg_file)

        # Params needed for https://github.com/truera/trulens/issues/470
        # Params are from https://stackoverflow.com/questions/55457069/how-to-fix-operationalerror-psycopg2-operationalerror-server-closed-the-conn
        stg_engine = create_engine(
            f"sqlite:///{stg_file}",
            pool_size=10,
            max_overflow=2,
            pool_recycle=300,
            pool_pre_ping=True,
            pool_use_lifo=True
        )
        upgrade_db(stg_engine, revision="1", prefix=prefix)

        # 3. Copy records from original database to staging
        src_conn = sqlite3.connect(original_file)
        tgt_conn = sqlite3.connect(stg_file)

        for table in ["apps", "feedback_defs", "records", "feedbacks"]:
            logger.info("Copying table '%s'", table)
            df = pd.read_sql(f"SELECT * FROM {table}", src_conn)
            for col in ["ts", "last_ts"]:
                if col in df:
                    df[col] = df[col].apply(
                        lambda ts: coerce_ts(ts).timestamp()
                    )
            logger.debug("\n\n%s\n", df.head())
            df.to_sql(table, tgt_conn, index=False, if_exists="append")

        # 4. Migrate staging database to the latest Alembic revision
        logger.debug("Applying Alembic migration scripts")
        upgrade_db(stg_engine, revision="head", prefix=prefix)

        # 5. Replace original database file with the staging one
        logger.debug("Replacing database file at %s", original_file)
        shutil.copyfile(stg_file, original_file)


def coerce_ts(ts: Union[datetime, str, int, float]) -> datetime:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        return datetime.fromisoformat(ts)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts)
    raise ValueError(f"Cannot coerce to datetime: {ts}")


def copy_database(
    src_url: str,
    tgt_url: str,
    src_prefix: str = mod_db.DEFAULT_DATABASE_PREFIX,
    tgt_prefix: str = mod_db.DEFAULT_DATABASE_PREFIX
):
    """
    Copy all data from a source database to an EMPTY target database.

    Important considerations:
        - All source data will be appended to the target tables, so it is
          important that the target database is empty.

        - Will fail if the databases are not at the latest schema revision. That
          can be fixed with `Tru(database_url="...", database_prefix="...").migrate_database()`

        - Might fail if the target database enforces relationship constraints,
          because then the order of inserting data matters.

        - This process is NOT transactional, so it is highly recommended that
          the databases are NOT used by anyone while this process runs.
    """

    from trulens_eval.database.sqlalchemy import SqlAlchemyDB

    src = SqlAlchemyDB.from_db_url(src_url, prefix=src_prefix)
    check_db_revision(src.engine, prefix=src_prefix)

    tgt = SqlAlchemyDB.from_db_url(tgt_url, prefix=tgt_prefix)
    check_db_revision(tgt.engine, prefix=tgt_prefix)

    for k, source_table_class in src.orm.registry.items():
        # ["apps", "feedback_defs", "records", "feedbacks"]:

        if not hasattr(source_table_class, "_table_base_name"):
            continue

        target_table_class = tgt.orm.registry.get(k)

        with src.engine.begin() as src_conn:

            with tgt.engine.begin() as tgt_conn:

                df = pd.read_sql(f"SELECT * FROM {source_table_class.__tablename__}", src_conn)
                df.to_sql(target_table_class.__tablename__, tgt_conn, index=False, if_exists="append")
