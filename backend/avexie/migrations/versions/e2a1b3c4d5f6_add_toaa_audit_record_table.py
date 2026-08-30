"""Add toaa_audit_record table

Revision ID: e2a1b3c4d5f6
Revises: d4c1a8e37b62
Create Date: 2026-08-30 00:00:00.000000

Append-only audit trail for tool calls (CONTRACTS.md §1).
Includes BEFORE UPDATE triggers that reject mutations to immutable
columns (tool_input, tool_name, created_at) on both Postgres and SQLite.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from avexie.migrations.util import get_existing_tables

revision: str = 'e2a1b3c4d5f6'
down_revision: Union[str, None] = 'd4c1a8e37b62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing_tables = set(get_existing_tables())

    if 'toaa_audit_record' not in existing_tables:
        op.create_table(
            'toaa_audit_record',
            sa.Column('id', sa.Text(), nullable=False, primary_key=True),
            sa.Column('session_id', sa.Text(), nullable=False),
            sa.Column('user_id', sa.Text(), nullable=False),
            sa.Column('tool_name', sa.Text(), nullable=False),
            sa.Column('tool_input', sa.Text(), nullable=False),
            sa.Column('tool_output', sa.Text(), nullable=True),
            sa.Column('status', sa.Text(), nullable=False, server_default='pending'),
            sa.Column('requires_approval', sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.Column('created_at', sa.BigInteger(), nullable=False),
            sa.Column('completed_at', sa.BigInteger(), nullable=True),
            sa.Column('error_detail', sa.Text(), nullable=True),
        )

        op.create_index('ix_toaa_audit_record_session_id', 'toaa_audit_record', ['session_id'])
        op.create_index('ix_toaa_audit_record_user_id', 'toaa_audit_record', ['user_id'])
        op.create_index('ix_toaa_audit_record_status', 'toaa_audit_record', ['status'])
        op.create_index('ix_toaa_audit_record_created_at', 'toaa_audit_record', ['created_at'])

    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == 'sqlite':
        bind.execute(sa.text('''
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
    else:
        bind.execute(sa.text('''
            CREATE OR REPLACE FUNCTION toaa_audit_immutable_guard()
            RETURNS TRIGGER AS $$
            BEGIN
                IF OLD.tool_input IS DISTINCT FROM NEW.tool_input THEN
                    RAISE EXCEPTION 'TOAA: tool_input is immutable after insert';
                END IF;
                IF OLD.tool_name IS DISTINCT FROM NEW.tool_name THEN
                    RAISE EXCEPTION 'TOAA: tool_name is immutable after insert';
                END IF;
                IF OLD.created_at IS DISTINCT FROM NEW.created_at THEN
                    RAISE EXCEPTION 'TOAA: created_at is immutable after insert';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        '''))
        bind.execute(sa.text('''
            DROP TRIGGER IF EXISTS toaa_audit_immutable_columns ON toaa_audit_record;
        '''))
        bind.execute(sa.text('''
            CREATE TRIGGER toaa_audit_immutable_columns
            BEFORE UPDATE ON toaa_audit_record
            FOR EACH ROW
            EXECUTE FUNCTION toaa_audit_immutable_guard();
        '''))


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == 'sqlite':
        bind.execute(sa.text('DROP TRIGGER IF EXISTS toaa_audit_immutable_columns;'))
    else:
        bind.execute(sa.text('DROP TRIGGER IF EXISTS toaa_audit_immutable_columns ON toaa_audit_record;'))
        bind.execute(sa.text('DROP FUNCTION IF EXISTS toaa_audit_immutable_guard();'))

    op.drop_index('ix_toaa_audit_record_created_at', table_name='toaa_audit_record')
    op.drop_index('ix_toaa_audit_record_status', table_name='toaa_audit_record')
    op.drop_index('ix_toaa_audit_record_user_id', table_name='toaa_audit_record')
    op.drop_index('ix_toaa_audit_record_session_id', table_name='toaa_audit_record')
    op.drop_table('toaa_audit_record')
