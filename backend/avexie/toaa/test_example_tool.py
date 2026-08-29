"""End-to-end test for the example echo tool wrapped with toaa_wrap.

Demonstrates the full audit lifecycle other tracks should expect:
  - pending row written BEFORE tool execution
  - row completed to success/error AFTER tool execution
  - original tool result returned unchanged

Uses sys.modules patching to avoid the avexie.env import chain
(WEBUI_SECRET_KEY etc.) — same isolation strategy as test_toaa.py.
"""

import copy
import sys
import time
import types
import uuid

import pytest

# ── Fake modules to break the avexie.env import chain ──

_fake_models = types.ModuleType('avexie.toaa.models')

_records: dict = {}


class FakeModel:
    def __init__(self, d):
        self.__dict__.update(d)


class FakeAuditTable:
    async def insert_pending(self, *, tool_name, tool_input, session_id, user_id, requires_approval=False):
        record_id = str(uuid.uuid4())
        record = {
            'id': record_id,
            'tool_name': tool_name,
            'tool_input': tool_input,
            'session_id': session_id,
            'user_id': user_id,
            'status': 'pending',
            'requires_approval': requires_approval,
            'created_at': int(time.time() * 1000),
            'completed_at': None,
            'tool_output': None,
            'error_detail': None,
        }
        _records[record_id] = record
        return FakeModel(record)

    async def complete(self, record_id, *, status, tool_output=None, error_detail=None):
        record = _records.get(record_id)
        if record:
            record['status'] = status
            record['tool_output'] = tool_output
            record['error_detail'] = error_detail
            record['completed_at'] = int(time.time() * 1000)


_fake_audit = FakeAuditTable()
_fake_models.ToaaAudit = _fake_audit
_fake_models.ToaaAuditRecordModel = None

# Patch sys.modules BEFORE importing wrapper or example_tool
sys.modules.setdefault('avexie.toaa.models', _fake_models)

from avexie.toaa.wrapper import toaa_wrap  # noqa: E402
from avexie.toaa.example_tool import echo, echo_audited  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_records():
    _records.clear()
    yield
    _records.clear()


@pytest.mark.asyncio
async def test_echo_success():
    """Successful echo call produces a pending->success audit trail."""
    result = await echo_audited(
        message='hello world',
        session_id='sess-example',
        user_id='user-42',
    )

    assert result == {'echoed': 'hello world'}

    assert len(_records) == 1
    record = list(_records.values())[0]
    assert record['tool_name'] == 'echo'
    assert record['status'] == 'success'
    assert record['tool_input'] == {'message': 'hello world'}
    assert record['completed_at'] is not None
    assert record['error_detail'] is None


@pytest.mark.asyncio
async def test_echo_error():
    """Failed echo call produces a pending->error audit trail and re-raises."""
    with pytest.raises(ValueError, match='must not be empty'):
        await echo_audited(
            message='',
            session_id='sess-example',
            user_id='user-42',
        )

    assert len(_records) == 1
    record = list(_records.values())[0]
    assert record['tool_name'] == 'echo'
    assert record['status'] == 'error'
    assert 'must not be empty' in record['error_detail']
    assert record['completed_at'] is not None


@pytest.mark.asyncio
async def test_echo_audit_row_exists_before_execution():
    """The pending row is visible BEFORE the tool function runs."""
    seen_during_execution = []

    async def spying_echo(**kwargs):
        seen_during_execution.append(copy.deepcopy(dict(_records)))
        return {'echoed': kwargs.get('message', '')}

    await toaa_wrap(
        tool_name='echo',
        tool_input={'message': 'spy'},
        tool_fn=spying_echo,
        session_id='sess-spy',
        user_id='user-spy',
    )

    assert len(seen_during_execution) == 1
    snapshot = seen_during_execution[0]
    assert len(snapshot) == 1
    record = list(snapshot.values())[0]
    assert record['status'] == 'pending'
