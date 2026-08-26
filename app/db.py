from datetime import datetime
from pathlib import Path

from sqlalchemy import DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .config import settings

if settings.database_url.startswith("sqlite:///./"):
    Path("data").mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)


class Base(DeclarativeBase):
    pass


class StoredFile(Base):
    __tablename__ = "stored_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    link_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    owner_telegram_id: Mapped[int] = mapped_column(Integer, index=True)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True, index=True)


def utcnow() -> datetime:
    return datetime.utcnow()


def init_db() -> None:
    Base.metadata.create_all(engine)


def add_file(record: StoredFile) -> None:
    with Session(engine) as session:
        session.add(record)
        session.commit()


def get_by_token(token: str) -> StoredFile | None:
    with Session(engine) as session:
        return session.scalar(select(StoredFile).where(StoredFile.link_token == token))


def expired_files(now: datetime | None = None) -> list[StoredFile]:
    now = now or utcnow()
    with Session(engine) as session:
        return list(session.scalars(select(StoredFile).where(StoredFile.expires_at.is_not(None), StoredFile.expires_at <= now)))


def delete_record(file_id: int) -> None:
    with Session(engine) as session:
        row = session.get(StoredFile, file_id)
        if row:
            session.delete(row)
            session.commit()
