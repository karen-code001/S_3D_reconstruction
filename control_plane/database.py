from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


database_url = get_settings().database_url
connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
engine = create_engine(database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

UPGRADE_COLUMNS = {
    "plant_type": "VARCHAR(20)",
    "task_name": "VARCHAR(80)",
    "group_name": "VARCHAR(50)",
    "uploaded_at": "TIMESTAMP WITH TIME ZONE",
}


def ensure_task_columns(bind=engine):
    """Upgrade existing databases without changing legacy task records."""
    with bind.begin() as connection:
        if bind.dialect.name == "postgresql":
            for name, column_type in UPGRADE_COLUMNS.items():
                connection.execute(text(
                    f"ALTER TABLE reconstruction_tasks ADD COLUMN IF NOT EXISTS {name} {column_type}"
                ))
        else:
            if bind.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            columns = {column["name"] for column in inspect(connection).get_columns("reconstruction_tasks")}
            for name, column_type in UPGRADE_COLUMNS.items():
                if name not in columns:
                    connection.execute(text(f"ALTER TABLE reconstruction_tasks ADD COLUMN {name} {column_type}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
