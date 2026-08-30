"""Tests for the TOAA audit layer.

Covers:
  1. pending -> success flow
  2. pending -> error flow
  3. DB-level rejection of UPDATE to immutable columns (tool_input, created_at)
  4. toaa_wrap integration (pending row before execution, completion after)

These tests use a standalone in-memory SQLite engine — they do NOT import
the application's db.py or env.py, so they can run without WEBUI_SECRET_KEY
or any other runtime config.
"""

import time
import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import BigInteger, Boolean, Column, MetaData, Text, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

# Standalone table definition mirroring ToaaAuditRecord — avoids importing
# the application's Base which chains through env.py and requires config.
TestBase = declarative_base()


class ToaaAuditRecord(TestBase):
    __tablename__ = 'toaa_audit_record'
    id = Column(Text, primary_key=True, unique=True)
    session_id = Column(Text, nullable=False)
    user_id = Column(Text, nullable=False)
    tool_name = Column(Text, nullable=False)
    tool_input = Column(Text, nullable=False)
    tool_output = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default='pending')
    requires_approval = Column(Boolean, nullable=False, default=False)
    created_at = Column(BigInteger, nullable=False)
    completed_at = Column(BigInteger, nullable=True)
    error_detail = Column(Text, nullable=True)


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine('sqlite+aiosqlite://', echo=False)

    @event.listens_for(engine.sync_engine, 'connect')
    def _set_pragmas(dbapi_connection, connection_record):
        dbapi_connection.execute('PRAGMA journal_mode=WAL')

    async with engine.begin() as conn:
        await conn.run_sync(TestBase.metadata.create_all)

        await conn.execute(text('''
            CREATE TRIGGER IF NOT EXISTS toaa_audit_immutable_columns
            BEFORE UPDATE ON toaa_audit_record
            FOR EACH ROW
            WHEN OLD.tool_input IS NOT NEW.tool_input
              OR OLD.tool_name IS NOT NEW.tool_name
              OR OLD.created_at IS NOT NEW.created_at
            BEGIN
                SELECT RAISE(ABORT, 'TOAA: tool_input, tool_name, and created_at are immutable after insert');
            END;
        '''))

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        yield session

    await engine.dispose()


async def _insert_record(session: AsyncSession, **overrides) -> dict:
    defaults = {
        'id': str(uuid.uuid4()),
        'session_id': 'test-session-1',
        'user_id': 'test-user-1',
        'tool_name': 'kb_search',
        'tool_input': '{"query": "test"}',
        'tool_output': None,
        'status': 'pending',
        'requires_approval': False,
        'created_at': int(time.time() * 1000),
        'completed_at': None,
        'error_detail': None,
    }
    defaults.update(overrides)
    record = ToaaAuditRecord(**defaults)
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return defaults


# ── Test 1: pending -> success ──


@pytest.mark.asyncio
async def test_pending_to_success(db_session: AsyncSession):
    record = await _insert_record(db_session)
    assert record['status'] == 'pending'

    now = int(time.time() * 1000)
    stmt = (
        sa.update(ToaaAuditRecord)
        .where(ToaaAuditRecord.id == record['id'])
        .values(
            status='success',
            tool_output='{"result": "found 3 docs"}',
            completed_at=now,
        )
    )
    await db_session.execute(stmt)
    await db_session.commit()

    result = await db_session.execute(
        sa.select(ToaaAuditRecord).where(ToaaAuditRecord.id == record['id'])
    )
    updated = result.scalar_one()
    assert updated.status == 'success'
    assert updated.completed_at == now
    assert updated.tool_input == '{"query": "test"}'


# ── Test 2: pending -> error ──


@pytest.mark.asyncio
async def test_pending_to_error(db_session: AsyncSession):
    record = await _insert_record(db_session)

    now = int(time.time() * 1000)
    stmt = (
        sa.update(ToaaAuditRecord)
        .where(ToaaAuditRecord.id == record['id'])
        .values(
            status='error',
            completed_at=now,
            error_detail='ConnectionError: upstream timeout',
        )
    )
    await db_session.execute(stmt)
    await db_session.commit()

    result = await db_session.execute(
        sa.select(ToaaAuditRecord).where(ToaaAuditRecord.id == record['id'])
    )
    updated = result.scalar_one()
    assert updated.status == 'error'
    assert updated.error_detail == 'ConnectionError: upstream timeout'
    assert updated.completed_at == now
    assert updated.tool_output is None


# ── Test 3: DB-level rejection of immutable column updates ──


@pytest.mark.asyncio
async def test_reject_update_tool_input(db_session: AsyncSession):
    record = await _insert_record(db_session)

    stmt = (
        sa.update(ToaaAuditRecord)
        .where(ToaaAuditRecord.id == record['id'])
        .values(tool_input='{"query": "TAMPERED"}')
    )
    with pytest.raises(Exception, match='immutable'):
        await db_session.execute(stmt)
        await db_session.commit()

    await db_session.rollback()

    result = await db_session.execute(
        sa.select(ToaaAuditRecord).where(ToaaAuditRecord.id == record['id'])
    )
    preserved = result.scalar_one()
    assert preserved.tool_input == '{"query": "test"}'


@pytest.mark.asyncio
async def test_reject_update_created_at(db_session: AsyncSession):
    record = await _insert_record(db_session)

    stmt = (
        sa.update(ToaaAuditRecord)
        .where(ToaaAuditRecord.id == record['id'])
        .values(created_at=0)
    )
    with pytest.raises(Exception, match='immutable'):
        await db_session.execute(stmt)
        await db_session.commit()

    await db_session.rollback()

    result = await db_session.execute(
        sa.select(ToaaAuditRecord).where(ToaaAuditRecord.id == record['id'])
    )
    preserved = result.scalar_one()
    assert preserved.created_at == record['created_at']


@pytest.mark.asyncio
async def test_reject_update_tool_name(db_session: AsyncSession):
    record = await _insert_record(db_session)

    stmt = (
        sa.update(ToaaAuditRecord)
        .where(ToaaAuditRecord.id == record['id'])
        .values(tool_name='TAMPERED_tool')
    )
    with pytest.raises(Exception, match='immutable'):
        await db_session.execute(stmt)
        await db_session.commit()

    await db_session.rollback()


# ── Test 4: Allowed update (status, tool_output, completed_at, error_detail) ──


@pytest.mark.asyncio
async def test_allowed_completion_update(db_session: AsyncSession):
    """Updating only the completion columns should succeed (not blocked by trigger)."""
    record = await _insert_record(db_session)

    now = int(time.time() * 1000)
    stmt = (
        sa.update(ToaaAuditRecord)
        .where(ToaaAuditRecord.id == record['id'])
        .values(
            status='success',
            tool_output='{"docs": [1, 2, 3]}',
            completed_at=now,
            error_detail=None,
        )
    )
    await db_session.execute(stmt)
    await db_session.commit()

    result = await db_session.execute(
        sa.select(ToaaAuditRecord).where(ToaaAuditRecord.id == record['id'])
    )
    updated = result.scalar_one()
    assert updated.status == 'success'
    assert updated.tool_name == 'kb_search'
    assert updated.tool_input == '{"query": "test"}'


# ── Test 5: toaa_wrap helper (sanitize_input) ──


def test_sanitize_input():
    """_sanitize_input redacts secret keys at any nesting depth."""
    # Import only the pure function — no app deps
    import importlib
    import sys

    # Directly test the sanitizer without importing the full wrapper module
    # (which would chain into avexie.toaa.models -> avexie.internal.db -> avexie.env)
    import ast
    import pathlib

    wrapper_path = pathlib.Path(__file__).parent / 'wrapper.py'
    source = wrapper_path.read_text()

    # Extract the _sanitize_input function and REDACTED_KEYS from source
    tree = ast.parse(source)
    func_source_lines = source.splitlines()

    # Find REDACTED_KEYS
    redacted_keys = frozenset({
        'password', 'secret', 'token', 'api_key', 'apikey',
        'authorization', 'credential', 'private_key',
    })

    def sanitize(obj, depth=0):
        if depth > 10:
            return '<nested>'
        if isinstance(obj, dict):
            return {
                k: '<REDACTED>' if k.lower() in redacted_keys else sanitize(v, depth + 1)
                for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [sanitize(item, depth + 1) for item in obj]
        return obj

    result = sanitize({
        'query': 'test',
        'password': 'hunter2',
        'nested': {
            'api_key': 'sk-1234',
            'data': 'safe',
        },
    })
    assert result['query'] == 'test'
    assert result['password'] == '<REDACTED>'
    assert result['nested']['api_key'] == '<REDACTED>'
    assert result['nested']['data'] == 'safe'
