"""Judge0-backed, no-egress code execution tool."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


JUDGE0_URL = os.getenv('JUDGE0_URL', 'http://judge0:2358').rstrip('/')

LANGUAGE_IDS = {
    'bash': 46,
    'c': 50,
    'c++': 54,
    'cpp': 54,
    'go': 60,
    'java': 62,
    'javascript': 63,
    'js': 63,
    'python': 71,
    'python3': 71,
    'rust': 73,
    'typescript': 74,
    'ts': 74,
}

DEFAULT_LIMITS = {
    'cpu_time': 2.0,
    'wall_time': 5.0,
    'memory_kb': 512_000,
    'max_processes': 30,
}

LIMIT_RANGES = {
    'cpu_time': (0.1, 2.0),
    'wall_time': (0.1, 5.0),
    'memory_kb': (16_384, 512_000),
    'max_processes': (1, 30),
}

MAX_SOURCE_BYTES = 100_000
MAX_STDIN_BYTES = 100_000
TERMINAL_STATUS_IDS = frozenset(range(3, 15))


def _validate_text(name: str, value: str, maximum_bytes: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    if len(value.encode('utf-8')) > maximum_bytes:
        raise ValueError(f'{name} exceeds the {maximum_bytes}-byte limit')


def _validated_limits(limits: Optional[dict]) -> dict[str, float | int]:
    if limits is None:
        return DEFAULT_LIMITS.copy()
    if not isinstance(limits, dict):
        raise TypeError('limits must be an object')

    unknown = sorted(set(limits) - set(DEFAULT_LIMITS))
    if unknown:
        raise ValueError(f'Unsupported limit(s): {", ".join(unknown)}')

    resolved = DEFAULT_LIMITS.copy()
    for name, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f'limits.{name} must be a number')
        minimum, maximum = LIMIT_RANGES[name]
        if not minimum <= value <= maximum:
            raise ValueError(f'limits.{name} must be between {minimum} and {maximum}')
        resolved[name] = int(value) if name in {'memory_kb', 'max_processes'} else float(value)
    return resolved


def _request_json(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, str]] = None,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    query = f'?{urlencode(params)}' if params else ''
    body = json.dumps(payload).encode('utf-8') if payload is not None else None
    request = Request(
        f'{JUDGE0_URL}{path}{query}',
        data=body,
        headers={'Content-Type': 'application/json'} if body is not None else {},
        method=method,
    )

    try:
        # Judge0 is an internal service; never inherit HTTP(S)_PROXY for this hop.
        with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
            response_body = response.read()
            response_status = response.status
    except HTTPError as exc:
        error_body = exc.read().decode('utf-8', 'replace')[:500]
        raise RuntimeError(f'Judge0 request failed ({exc.code}): {error_body}') from exc

    try:
        result = json.loads(response_body)
    except (TypeError, ValueError) as exc:
        invalid_body = response_body.decode('utf-8', 'replace')[:500]
        raise RuntimeError(f'Judge0 returned an invalid response ({response_status}): {invalid_body}') from exc
    if not isinstance(result, dict):
        raise RuntimeError('Judge0 returned a non-object response')
    return result


def _execution_result(submission: dict[str, Any]) -> dict[str, Any]:
    status = submission.get('status') or {}
    status_id = status.get('id')
    description = str(status.get('description') or 'Unknown')

    stderr_parts = [submission.get('compile_output'), submission.get('stderr')]
    stderr = '\n'.join(str(part).rstrip() for part in stderr_parts if part).strip()

    execution_time = submission.get('time')
    try:
        execution_time = float(execution_time) if execution_time is not None else None
    except (TypeError, ValueError):
        execution_time = None

    return {
        'status': 'done',
        'stdout': submission.get('stdout') or '',
        'stderr': stderr,
        'exit_code': submission.get('exit_code'),
        'execution_time': execution_time,
        'status_id': status_id,
        'status_description': description,
        'timed_out': status_id == 5,
        'memory_kb': submission.get('memory'),
    }


async def run_code(
    language: str,
    source: str,
    stdin: str = '',
    limits: Optional[dict] = None,
) -> dict:
    """Run source code in the self-hosted, no-egress Judge0 sandbox.

    Supported languages are Python, JavaScript, TypeScript, Bash, C, C++, Java,
    Go, and Rust. Requested limits may only reduce the deployment-wide caps.

    :param language: Language name or alias, for example "python", "javascript", or "cpp"
    :param source: Source code to execute
    :param stdin: Optional standard input supplied to the program
    :param limits: Optional object with cpu_time, wall_time, memory_kb, and max_processes
    :return: Object containing stdout, stderr, exit_code, execution_time, and terminal status
    """
    if not isinstance(language, str):
        raise TypeError('language must be a string')
    language_id = LANGUAGE_IDS.get(language.strip().lower())
    if language_id is None:
        supported = ', '.join(sorted(LANGUAGE_IDS))
        raise ValueError(f'Unsupported language "{language}". Supported values: {supported}')

    _validate_text('source', source, MAX_SOURCE_BYTES)
    _validate_text('stdin', stdin, MAX_STDIN_BYTES)
    if not source.strip():
        raise ValueError('source must not be empty')
    resolved_limits = _validated_limits(limits)

    submission = {
        'language_id': language_id,
        'source_code': source,
        'stdin': stdin,
        'cpu_time_limit': resolved_limits['cpu_time'],
        'wall_time_limit': resolved_limits['wall_time'],
        'memory_limit': resolved_limits['memory_kb'],
        'max_processes_and_or_threads': resolved_limits['max_processes'],
        'enable_per_process_and_thread_time_limit': True,
        'enable_per_process_and_thread_memory_limit': True,
        'enable_network': False,
        'number_of_runs': 1,
    }

    wall_time = float(resolved_limits['wall_time'])
    deadline = time.monotonic() + wall_time + 10
    fields = 'stdout,stderr,compile_output,exit_code,time,wall_time,memory,status'

    try:
        created = await asyncio.to_thread(
            _request_json,
            'POST',
            '/submissions',
            params={'base64_encoded': 'false', 'wait': 'false'},
            payload=submission,
        )

        token = created.get('token')
        if not token:
            raise RuntimeError(f'Judge0 did not return a submission token: {created}')

        while time.monotonic() < deadline:
            result = await asyncio.to_thread(
                _request_json,
                'GET',
                f'/submissions/{token}',
                params={'base64_encoded': 'false', 'fields': fields},
            )

            status_id = (result.get('status') or {}).get('id')
            if status_id in TERMINAL_STATUS_IDS:
                return _execution_result(result)
            await asyncio.sleep(0.1)
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f'Judge0 is unavailable at {JUDGE0_URL}: {exc}') from exc

    raise RuntimeError('Judge0 did not finish the submission before the sandbox deadline')
