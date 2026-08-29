"""Integration tests for the no-egress Judge0 run_code tool."""

import importlib.util
import unittest
from pathlib import Path


RUN_CODE_PATH = Path(__file__).parents[2] / 'avexie' / 'tools' / 'run_code.py'
RUN_CODE_SPEC = importlib.util.spec_from_file_location('avexie_run_code', RUN_CODE_PATH)
if RUN_CODE_SPEC is None or RUN_CODE_SPEC.loader is None:
    raise RuntimeError(f'Unable to load run_code module at {RUN_CODE_PATH}')
RUN_CODE_MODULE = importlib.util.module_from_spec(RUN_CODE_SPEC)
RUN_CODE_SPEC.loader.exec_module(RUN_CODE_MODULE)
run_code = RUN_CODE_MODULE.run_code


class RunCodeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_passing_snippet(self):
        result = await run_code(
            language='python',
            source='name = input()\nprint(f"hello {name}")',
            stdin='AVEXIE\n',
        )

        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['stdout'], 'hello AVEXIE\n')
        self.assertEqual(result['stderr'], '')
        self.assertEqual(result['exit_code'], 0)
        self.assertIsInstance(result['execution_time'], float)

    async def test_preserves_nonzero_exit(self):
        result = await run_code(
            language='python',
            source='import sys\nprint("expected failure", file=sys.stderr)\nraise SystemExit(7)',
        )

        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['exit_code'], 7)
        self.assertIn('expected failure', result['stderr'])

    async def test_kills_infinite_loop_at_limit(self):
        result = await run_code(
            language='python',
            source='while True:\n    pass',
            limits={'cpu_time': 0.5, 'wall_time': 2},
        )

        self.assertEqual(result['status'], 'done')
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['status_description'], 'Time Limit Exceeded')

    async def test_blocks_network_access(self):
        result = await run_code(
            language='python',
            source='''
import socket
import sys

try:
    connection = socket.create_connection(("1.1.1.1", 80), timeout=1)
except OSError as exc:
    print(f"network blocked: {type(exc).__name__}")
    raise SystemExit(17)
else:
    connection.close()
    print("network unexpectedly reachable")
    raise SystemExit(99)
'''.strip(),
            limits={'wall_time': 3},
        )

        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['exit_code'], 17)
        self.assertIn('network blocked:', result['stdout'])
