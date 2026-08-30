import logging
import time
import uuid
from typing import Optional

from avexie.internal.db import Base, JSONField, get_async_db_context
from pydantic import BaseModel, ConfigDict
from sqlalchemy import BigInteger, Boolean, Column, Text, select, update
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


####################
# ToaaAuditRecord DB Schema
####################


class ToaaAuditRecord(Base):
    __tablename__ = 'toaa_audit_record'

    id = Column(Text, primary_key=True, unique=True)
    session_id = Column(Text, nullable=False)
    user_id = Column(Text, nullable=False)
    tool_name = Column(Text, nullable=False)
    tool_input = Column(JSONField, nullable=False)
    tool_output = Column(JSONField, nullable=True)
    status = Column(Text, nullable=False, default='pending')
    requires_approval = Column(Boolean, nullable=False, default=False)
    created_at = Column(BigInteger, nullable=False)
    completed_at = Column(BigInteger, nullable=True)
    error_detail = Column(Text, nullable=True)


class ToaaAuditRecordModel(BaseModel):
    id: str
    session_id: str
    user_id: str
    tool_name: str
    tool_input: dict
    tool_output: Optional[dict] = None
    status: str
    requires_approval: bool = False
    created_at: int
    completed_at: Optional[int] = None
    error_detail: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


####################
# Repository
####################


class ToaaAuditTable:
    async def insert_pending(
        self,
        *,
        tool_name: str,
        tool_input: dict,
        session_id: str,
        user_id: str,
        requires_approval: bool = False,
        db: Optional[AsyncSession] = None,
    ) -> Optional[ToaaAuditRecordModel]:
        async with get_async_db_context(db) as db:
            record_id = str(uuid.uuid4())
            record = ToaaAuditRecord(
                id=record_id,
                session_id=session_id,
                user_id=user_id,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=None,
                status='pending',
                requires_approval=requires_approval,
                created_at=int(time.time() * 1000),
                completed_at=None,
                error_detail=None,
            )
            try:
                db.add(record)
                await db.commit()
                await db.refresh(record)
                return ToaaAuditRecordModel.model_validate(record)
            except Exception as e:
                log.exception(f'Failed to insert TOAA audit record: {e}')
                await db.rollback()
                return None

    async def complete(
        self,
        record_id: str,
        *,
        status: str,
        tool_output: Optional[dict] = None,
        error_detail: Optional[str] = None,
        db: Optional[AsyncSession] = None,
    ) -> Optional[ToaaAuditRecordModel]:
        if status not in ('success', 'error', 'blocked'):
            raise ValueError(f'Invalid completion status: {status!r}')

        async with get_async_db_context(db) as db:
            try:
                now = int(time.time() * 1000)
                stmt = (
                    update(ToaaAuditRecord)
                    .where(
                        ToaaAuditRecord.id == record_id,
                        ToaaAuditRecord.completed_at.is_(None),
                    )
                    .values(
                        status=status,
                        tool_output=tool_output,
                        completed_at=now,
                        error_detail=error_detail,
                    )
                )
                result = await db.execute(stmt)
                await db.commit()

                if result.rowcount == 0:
                    log.warning(f'TOAA audit record {record_id} not found or already completed')
                    return None

                row = await db.execute(
                    select(ToaaAuditRecord).where(ToaaAuditRecord.id == record_id)
                )
                record = row.scalar_one_or_none()
                return ToaaAuditRecordModel.model_validate(record) if record else None
            except Exception as e:
                log.exception(f'Failed to complete TOAA audit record {record_id}: {e}')
                await db.rollback()
                return None

    async def get_by_id(
        self, record_id: str, *, db: Optional[AsyncSession] = None
    ) -> Optional[ToaaAuditRecordModel]:
        async with get_async_db_context(db) as db:
            result = await db.execute(
                select(ToaaAuditRecord).where(ToaaAuditRecord.id == record_id)
            )
            record = result.scalar_one_or_none()
            return ToaaAuditRecordModel.model_validate(record) if record else None

    async def get_by_session(
        self, session_id: str, *, db: Optional[AsyncSession] = None
    ) -> list[ToaaAuditRecordModel]:
        async with get_async_db_context(db) as db:
            result = await db.execute(
                select(ToaaAuditRecord)
                .where(ToaaAuditRecord.session_id == session_id)
                .order_by(ToaaAuditRecord.created_at.asc())
            )
            return [ToaaAuditRecordModel.model_validate(r) for r in result.scalars().all()]


ToaaAudit = ToaaAuditTable()
