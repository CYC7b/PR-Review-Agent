"""数据库引擎与会话工厂（支持 SQLite / PostgreSQL）。"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import ensure_data_dir, get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        if url.startswith("sqlite"):
            ensure_data_dir()
        _engine = create_engine(
            url,
            future=True,
            echo=False,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _SessionLocal


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """事务作用域：成功提交，异常回滚。"""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖。"""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """创建所有表（开发/测试用）。生产环境使用 Alembic 迁移。"""
    from app.db.models import Base  # noqa: F401  确保模型已导入

    Base.metadata.create_all(get_engine())


def reset_engine() -> None:
    """重置引擎缓存（测试用）。"""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None
