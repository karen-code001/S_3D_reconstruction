from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

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
    "yolo_ini": "VARCHAR(200)",
    "segment": "BOOLEAN",
    "hidden": "BOOLEAN",
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


def seed_plants(bind=engine):
    """Fill an empty plants table with the built-in crops; never touches existing rows."""
    from common.plants import DEFAULT_PLANTS

    from .models import Plant

    with Session(bind) as db:
        if db.scalar(select(Plant.key).limit(1)) is not None:
            return
        db.add_all(
            Plant(
                key=seed.key,
                name_zh=seed.name_zh,
                name_en=seed.name_en,
                yolo_ini=seed.yolo_ini,
                segment=seed.segment,
                enabled=seed.enabled,
                sort_order=index * 10,
            )
            for index, seed in enumerate(DEFAULT_PLANTS)
        )
        db.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
