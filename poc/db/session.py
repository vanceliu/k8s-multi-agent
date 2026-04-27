"""Async SQLAlchemy session factory."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from poc.utils.config import DATABASE_URL

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    from poc.db.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session
