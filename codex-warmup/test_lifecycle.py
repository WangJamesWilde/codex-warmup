import concurrent.futures
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from engine import Store
from diagnostics import health
from resume import rules
import server
from test_plugin import NOW, snapshot


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent / '.state')
        self.store = Store(self.temp.name)
        self.store.put('enabled', True)
        self.store.put('bound_account', 'test-account')
        self.store.put('snapshot', snapshot(100, reset=NOW - 1))
        self.store.put('next_reset', NOW - 1)
        self.store.put('worker', {'pid': 123, 'heartbeat': NOW, 'state': 'checking'})
        self.store.put('worker_last_start', {'pid': 123, 'owner': 'old-worker'})
        self.store.lease('old-worker', NOW)

    def tearDown(self):
        self.temp.cleanup()

    def handshake(self, lines=None):
        lines = lines or [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        ]
        result = io.StringIO()
        with patch('server.sys.stdin', io.StringIO('\n'.join(json.dumps(x) for x in lines))), patch('server.sys.stdout', result):
            server.mcp(self.store)
        return [json.loads(line) for line in result.getvalue().splitlines()]

    def test_load_reclaims_only_confirmed_dead_owner_and_keeps_recovery_evidence(self):
        before = {key: self.store.get(key) for key in ('enabled', 'bound_account', 'snapshot', 'next_reset')}
        with patch('server.time.time', return_value=NOW), patch('server.process_alive', return_value=False), patch('server.subprocess.Popen') as popen:
            popen.return_value.pid = 456
            reply = self.handshake()
        popen.assert_called_once()
        self.assertEqual([row['id'] for row in reply], [1, 2])
        self.assertEqual(len(reply[1]['result']['tools']), 11)
        self.assertEqual(before, {key: self.store.get(key) for key in before})
        self.assertEqual(self.store.get('worker_last_launch')['worker_pid'], 456)
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT until FROM leases WHERE name='worker'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 0)
        self.assertEqual(rules(self.store), [])

    def test_paused_does_not_launch_or_reenable(self):
        self.store.put('enabled', False)
        with patch('server.subprocess.Popen') as popen:
            self.handshake()
        popen.assert_not_called()
        self.assertFalse(self.store.get('enabled'))

    def test_load_does_not_rebind_accounts_or_call_enable(self):
        with patch('server.time.time', return_value=NOW), patch('server.process_alive', return_value=False), patch('server.subprocess.Popen'), patch('server.enable') as enable, patch('server.Rpc') as rpc:
            server.recover_worker(self.store)
        enable.assert_not_called()
        rpc.assert_not_called()
        self.assertEqual(self.store.get('bound_account'), 'test-account')

    def test_live_worker_is_not_replaced_even_with_a_stale_heartbeat(self):
        self.store.put('worker', {'pid': 123, 'heartbeat': NOW - 500, 'state': 'checking'})
        with patch('server.process_alive', return_value=True), patch('server.subprocess.Popen') as popen:
            self.assertTrue(server.recover_worker(self.store)['already_running'])
        popen.assert_not_called()

    def test_unqueryable_process_is_not_replaced(self):
        with patch('server.process_alive', return_value=None), patch('server.subprocess.Popen') as popen:
            self.assertEqual(server.recover_worker(self.store)['not_started'], 'process_state_unknown')
        popen.assert_not_called()

    def test_unknown_lease_owner_is_not_stolen(self):
        self.store.put('worker_last_start', {'pid': 123, 'owner': 'different-owner'})
        with patch('server.time.time', return_value=NOW), patch('server.process_alive', return_value=False), patch('server.subprocess.Popen') as popen:
            self.assertEqual(server.recover_worker(self.store)['not_started'], 'worker_lease_unconfirmed')
        popen.assert_not_called()

    def test_expired_lease_allows_recovery(self):
        with patch('server.time.time', return_value=NOW + 300), patch('server.process_alive', return_value=False), patch('server.subprocess.Popen') as popen:
            popen.return_value.pid = 456
            self.assertEqual(server.recover_worker(self.store), {'started_pid': 456})
        popen.assert_called_once()

    def test_concurrent_chat_loads_reserve_one_launch(self):
        with patch('server.time.time', return_value=NOW), patch('server.process_alive', side_effect=lambda pid: pid == 456), patch('server.subprocess.Popen') as popen:
            popen.return_value.pid = 456
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: server.recover_worker(self.store), range(8)))
        popen.assert_called_once()
        self.assertEqual(sum('started_pid' in result for result in results), 1)

    def test_live_new_launch_is_not_duplicated_before_first_heartbeat(self):
        self.store.put('worker_last_launch', {'at': NOW - 30, 'worker_pid': 456})
        with patch('server.time.time', return_value=NOW), patch('server.process_alive', side_effect=lambda pid: pid == 456), patch('server.subprocess.Popen') as popen:
            self.assertEqual(server.recover_worker(self.store)['not_started'], 'launch_pending')
        popen.assert_not_called()

    def test_failed_boot_has_a_short_backoff_instead_of_a_load_storm(self):
        self.store.put('worker_last_launch', {'at': NOW, 'worker_pid': 456, 'launch_id': 'boot-attempt'})
        with self.store.db() as db:
            db.execute("INSERT INTO leases VALUES ('worker_launch','boot-attempt',?)", (NOW + 15,))
        with patch('server.time.time', return_value=NOW), patch('server.process_alive', return_value=False), patch('server.subprocess.Popen') as popen:
            self.assertEqual(server.recover_worker(self.store)['not_started'], 'launch_pending')
        popen.assert_not_called()

    def test_enabled_but_never_launched_can_start_without_a_pid(self):
        self.store.put('worker', None)
        with self.store.db() as db:
            db.execute("DELETE FROM leases WHERE name='worker'")
        with patch('server.process_alive', return_value=None), patch('server.subprocess.Popen') as popen:
            popen.return_value.pid = 456
            self.assertEqual(server.recover_worker(self.store), {'started_pid': 456})
        popen.assert_called_once()

    def test_launch_failure_does_not_break_handshake_and_throttles_other_chats(self):
        with patch('server.time.time', return_value=NOW), patch('server.process_alive', return_value=False), patch('server.subprocess.Popen', side_effect=OSError('private message')) as popen:
            reply = self.handshake()
            server.recover_worker(self.store)
        popen.assert_called_once()
        self.assertEqual([row['id'] for row in reply], [1, 2])
        self.assertEqual(self.store.get('worker_last_error')['stage'], 'plugin_load')
        self.assertNotIn('private', (self.store.root / 'diagnostics.jsonl').read_text(encoding='utf-8'))

    def test_premature_and_duplicate_notifications_do_not_restart_again(self):
        lines = [
            {'method': 'notifications/initialized'},
            {'id': 1, 'method': 'initialize'},
            {'method': 'notifications/initialized'},
            {'method': 'notifications/initialized'},
        ]
        with patch('server.recover_worker') as recover:
            self.handshake(lines)
        recover.assert_called_once_with(self.store)

    def test_status_and_refresh_are_not_restart_controls(self):
        snap = snapshot(100, reset=int(time.time()) + 18000)
        snap['secondary']['resetsAt'] = int(time.time()) + 604800
        self.store.put('snapshot', snap)
        rpc = Mock()
        rpc.__enter__ = Mock(return_value=rpc)
        rpc.__exit__ = Mock(return_value=False)
        rpc.snapshot.return_value = snapshot(5)
        before = self.store.get('snapshot')
        with patch('server.recover_worker') as recover, patch('server.Rpc', return_value=rpc):
            server.tool(self.store, 'warmup_status', {})
            server.tool(self.store, 'warmup_refresh', {})
        recover.assert_not_called()
        self.assertEqual(self.store.get('snapshot'), before)

    def test_unreadable_state_does_not_break_mcp_discovery(self):
        with patch.object(self.store, 'get', side_effect=OSError('private data')):
            reply = self.handshake()
        self.assertEqual([row['id'] for row in reply], [1, 2])
        self.assertNotIn('private', (self.store.root / 'diagnostics.jsonl').read_text(encoding='utf-8'))

    @unittest.skipUnless(os.name == 'nt', 'Windows process lifecycle')
    def test_real_fixture_crash_then_mcp_reload_recovers_once_without_any_model(self):
        self.store.put('worker', None)
        with self.store.db() as db:
            db.execute('DELETE FROM leases')
        before = self.store.get('snapshot')
        children = []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc
        def wait_heartbeat(pid):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                state = self.store.get('worker') or {}
                if state.get('pid') == pid and state.get('state') == 'lifecycle_fixture_only':
                    return
                time.sleep(.03)
            self.fail('Fixture failed to start')
        try:
            fixture = Path(__file__).parent / 'fixtures/no-model-worker.py'
            with patch('server.worker_script', return_value=fixture), patch('server.subprocess.Popen', side_effect=spawn):
                self.handshake()
                self.assertEqual(len(children), 1)
                wait_heartbeat(children[0].pid)
                children[0].kill()
                children[0].wait(timeout=5)
                self.assertEqual(health(self.store)['state'], 'process_exited')
                self.handshake()
                self.assertEqual(len(children), 2)
                wait_heartbeat(children[1].pid)
                self.assertEqual(health(self.store)['state'], 'healthy')
                self.handshake()
                self.assertEqual(len(children), 2)
                self.assertEqual(self.store.get('snapshot'), before)
                self.assertEqual(rules(self.store), [])
                with self.store.db() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 0)
        finally:
            self.store.put('enabled', False)
            for proc in children:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
