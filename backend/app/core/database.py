from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import get_settings

settings = get_settings()

# Production shares a managed Postgres cluster (max_connections = 50) with
# three other apps. Budget per process: pool_size=2, max_overflow=2 (see
# docs/deploy.md). Changing these numbers is a capacity call for the operator.
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=2,
    max_overflow=2,
    pool_recycle=1800,
    pool_timeout=30,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
