import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import logging
from ..config import settings

logger = logging.getLogger(__name__)


def _to_asyncpg_url(url: str) -> str:
    """Normalise a libpq style URL to the SQLAlchemy asyncpg dialect."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


class DatabasePool:
    def __init__(self):
        self.engine = None
        self.session_factory = None
        self._lock = asyncio.Lock()

    async def initialize(self):
        """Initialize database connection pool (idempotent)."""
        async with self._lock:
            if self.session_factory is not None:
                return

            # The connection string comes from DATABASE_URL (see docker-compose.yml).
            # It used to be assembled from settings that do not exist on Settings,
            # which raised AttributeError on every startup and left the pool unusable.
            database_url = _to_asyncpg_url(settings.database_url)

            self.engine = create_async_engine(
                database_url,
                # No explicit poolclass: an async engine needs an async-aware pool
                # (AsyncAdaptedQueuePool), which SQLAlchemy selects by default.
                pool_size=20,  # Number of connections to maintain
                max_overflow=30,  # Additional connections when needed
                pool_pre_ping=True,  # Validate connections
                pool_recycle=3600,  # Recycle connections every hour
                echo=False  # Set to True for SQL debugging
            )

            self.session_factory = async_sessionmaker(
                bind=self.engine,
                class_=AsyncSession,
                expire_on_commit=False
            )

            logger.info("✅ Database connection pool initialized")

    async def close(self):
        """Close database connections"""
        async with self._lock:
            if self.engine:
                await self.engine.dispose()
            self.engine = None
            self.session_factory = None

    @asynccontextmanager
    async def get_session(self) -> AsyncIterator[AsyncSession]:
        """Yield a database session from the pool.

        Exposed as an async context manager so that `async with pool.get_session()`
        works: returning a bare coroutine made every caller fail with
        `AttributeError: __aenter__`.
        """
        if not self.session_factory:
            raise RuntimeError("Database pool not initialized")
        async with self.session_factory() as session:
            yield session


# Global database pool instance
db_pool = DatabasePool()
