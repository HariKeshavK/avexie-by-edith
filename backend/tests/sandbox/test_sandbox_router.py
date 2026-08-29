from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from avexie.routers import sandbox


@pytest.mark.asyncio
async def test_execute_code_forwards_validated_submission(monkeypatch):
    captured = {}

    async def fake_run_code(**kwargs):
        captured.update(kwargs)
        return {
            'status': 'done',
            'stdout': 'hello\n',
            'stderr': '',
            'exit_code': 0,
            'execution_time': 0.01,
            'status_id': 3,
            'status_description': 'Accepted',
            'timed_out': False,
        }

    monkeypatch.setattr(sandbox, 'run_code', fake_run_code)

    result = await sandbox.execute_code(
        sandbox.RunCodeForm(
            language='python',
            source='print("hello")',
            stdin='input',
            limits=sandbox.SandboxLimits(cpu_time=1, wall_time=2),
        ),
        _user=SimpleNamespace(id='user-1'),
    )

    assert result['exit_code'] == 0
    assert captured == {
        'language': 'python',
        'source': 'print("hello")',
        'stdin': 'input',
        'limits': {'cpu_time': 1.0, 'wall_time': 2.0},
    }


@pytest.mark.asyncio
async def test_execute_code_returns_client_error_for_invalid_submission(monkeypatch):
    async def fake_run_code(**_kwargs):
        raise ValueError('Unsupported language')

    monkeypatch.setattr(sandbox, 'run_code', fake_run_code)

    with pytest.raises(HTTPException) as raised:
        await sandbox.execute_code(
            sandbox.RunCodeForm(language='unknown', source='hello'),
            _user=SimpleNamespace(id='user-1'),
        )

    assert raised.value.status_code == 400
