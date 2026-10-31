import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from diagnostics import failure, health, process_alive, record
from engine import Store, enable, tick, worker
from resume import rules, save_rule, dispatch
from runtime import MetadataError, Rpc
import server
from test_plugin import NOW, snapshot

THREAD = '11111111-1111-4111-8111-111111111111'


class Diagnostics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent / '.state')
        self.store = Store(self.temp.name)
        self.store.put('bound_account', 'test-account')
        self.store.put('enabled', True)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self):
        rpc = Mock()
        rpc.snapshot.return_value = snapshot(100, reset=19000, at=NOW - 100)
        rpc.model_available.return_value = True
        rpc.warmup.return_value = {'request_completed': True}
        self.turn = {'id': '22222222-2222-4222-8222-222222222222', 'status': 'failed', 'completedAt': NOW - 60,
                     'error': {'codexErrorInfo': 'usageLimitExceeded'}}
        rpc.call.side_effect = lambda method, params: (
            {'thread': {'id': THREAD, 'name': 'Fixture', 'model': 'original-model', 'status': {'type': 'idle'}}}
            if method == 'thread/read' else {'data': [self.turn]})
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=rpc)
        factory.return_value.__exit__ = Mock(return_value=False)
        self.store.put('snapshot', rpc.snapshot.return_value)
        self.store.put('next_reset', 19000)
        with patch('resume.time.time', return_value=NOW - 80):
            save_rule(self.store, {'thread_id': THREAD, 'message': 'fixture-only', 'confirm_send': True}, factory)
        self.sender = Mock(return_value={'queued': True, 'state': 'queued', 'execution_confirmed': False})
        return rpc, factory

    def enable_with(self, rpc, factory, fresh):
        rpc.snapshot.return_value = fresh
        with patch('engine.Rpc', factory), patch('engine.time.time', return_value=NOW):
            enable(self.store)

    def test_reenable_after_reset_records_recovery_and_resumes_exactly_once(self):
        rpc, factory = self.fixture()
        self.enable_with(rpc, factory, snapshot())
        self.assertEqual(self.store.get('resume_recovery')['at'], NOW)
        rpc.snapshot.side_effect = [snapshot(), snapshot(1), snapshot(1), snapshot(1)]
        with patch('resume.queue_message', self.sender), patch('resume.desktop_running', return_value=True), patch('engine.time.time', return_value=NOW):
            tick(self.store, factory, NOW)
            tick(self.store, factory, NOW)
        self.sender.assert_called_once_with(THREAD, 'fixture-only')
        rpc.warmup.assert_called_once()
        self.assertEqual(rules(self.store)[0]['remaining_runs'], 0)

    def test_reenable_after_another_chat_used_quota_resumes_without_warmup(self):
        rpc, factory = self.fixture()
        self.enable_with(rpc, factory, snapshot(5))
        with patch('resume.queue_message', self.sender), patch('resume.desktop_running', return_value=True):
            tick(self.store, factory, NOW)
        self.sender.assert_called_once()
        rpc.warmup.assert_not_called()

    def test_reenable_does_not_invent_recovery_without_previous_evidence(self):
        rpc, factory = self.fixture()
        self.store.put('snapshot', None)
        self.enable_with(rpc, factory, snapshot(5))
        self.assertIsNone(self.store.get('resume_recovery'))

    def test_reenable_on_new_account_does_not_reuse_old_recovery(self):
        rpc, factory = self.fixture()
        self.enable_with(rpc, factory, snapshot(5, account='other-account'))
        self.assertIsNone(self.store.get('resume_recovery'))
        self.assertEqual(rules(self.store)[0]['account'], 'test-account')

    def test_reenable_unchanged_quota_does_not_invent_a_transition(self):
        rpc, factory = self.fixture()
        self.store.put('snapshot', snapshot(5))
        self.store.put('next_reset', 37000)
        self.enable_with(rpc, factory, snapshot(5))
        self.assertIsNone(self.store.get('resume_recovery'))

    def test_manually_continued_chat_is_not_sent_again_after_reenable(self):
        rpc, factory = self.fixture()
        self.enable_with(rpc, factory, snapshot(5))
        self.turn.update(id='manual-turn', status='completed', error=None)
        with patch('resume.queue_message', self.sender), patch('resume.desktop_running', return_value=True):
            tick(self.store, factory, NOW)
        self.sender.assert_not_called()
        rule = rules(self.store)[0]
        self.assertEqual(rule['remaining_runs'], 1)
        self.assertEqual(self.store.get('resume_diagnostics')[rule['id']]['reason'], 'not_proven_quota_stop')

    def test_skip_diagnostics_do_not_change_rule_or_consume_count(self):
        rpc, _ = self.fixture()
        before = rules(self.store)
        dispatch(self.store, rpc, snapshot(50), NOW, self.sender, lambda: True)
        self.assertEqual(rules(self.store), before)
        self.assertEqual(self.store.get('resume_diagnostics')[before[0]['id']]['reason'], 'waiting_recovery_evidence')
        self.sender.assert_not_called()

    def test_recent_heartbeat_does_not_hide_a_missing_process(self):
        self.store.put('worker', {'pid': 123, 'heartbeat': NOW, 'state': 'checking'})
        with patch('diagnostics.process_alive', return_value=False):
            self.assertEqual(health(self.store, NOW)['state'], 'process_exited')

    def test_paused_and_stale_are_distinct(self):
        self.store.put('worker', {'pid': 123, 'heartbeat': NOW - 300, 'state': 'checking'})
        with patch('diagnostics.process_alive', return_value=True):
            self.assertEqual(health(self.store, NOW)['state'], 'stale')
            self.store.put('enabled', False)
            self.assertEqual(health(self.store, NOW)['state'], 'paused')

    def test_new_launch_has_a_short_starting_state(self):
        self.store.put('worker_last_launch', {'at': NOW, 'worker_pid': 456})
        with patch('diagnostics.process_alive', return_value=None):
            self.assertEqual(health(self.store, NOW)['state'], 'starting')
            self.assertEqual(health(self.store, NOW + 16)['state'], 'missing')

    @unittest.skipUnless(os.name == 'nt', 'Windows process metadata')
    def test_windows_process_query_recognizes_self_without_signals(self):
        self.assertTrue(process_alive(os.getpid()))

    def test_failure_never_retains_raw_exception_or_credentials(self):
        exc = RuntimeError('Bearer private-token user@example.invalid private conversation')
        exc.rpc_method = 'account/read'
        value = failure(exc, 'check')
        record(self.store, 'check_failed', value, 'worker_last_error')
        text = (self.store.root / 'diagnostics.jsonl').read_text(encoding='utf-8')
        self.assertNotIn('private', text)
        self.assertNotIn('example.invalid', text)
        self.assertEqual(self.store.get('worker_last_error')['method'], 'account/read')

    def test_diagnostic_file_survives_sqlite_logging_failure(self):
        with patch.object(self.store, 'put', side_effect=OSError()), patch.object(self.store, 'event', side_effect=OSError()):
            record(self.store, 'worker_failed', {'type': 'OSError', 'stage': 'worker'}, 'worker_last_error')
        self.assertTrue((self.store.root / 'diagnostics.jsonl').is_file())

    def test_diagnostic_log_has_bounded_rotation(self):
        with patch.object(self.store, 'event'):
            for _ in range(5):
                record(self.store, 'fixture', {'padding': 'x' * 200000})
        logs = list(self.store.root.glob('diagnostics.jsonl*'))
        self.assertLessEqual(len(logs), 3)
        self.assertLessEqual(sum(file.stat().st_size for file in logs), 3 * 262144)

    def test_worker_records_start_and_clean_exit(self):
        def stop(_):
            self.store.put('enabled', False)
            return 'paused'
        with patch('engine.tick', side_effect=stop):
            worker(self.store)
        self.assertIsNotNone(self.store.get('worker_last_start'))
        self.assertEqual(self.store.get('worker_last_exit')['reason'], 'paused')
        self.assertEqual(self.store.get('worker')['state'], 'stopped')

    def test_worker_keeps_rpc_stage_and_error_code_without_error_body(self):
        with patch('engine.tick', side_effect=MetadataError('account/rateLimits/read', -32602)), patch(
                'engine.time.sleep', side_effect=lambda _: self.store.put('enabled', False)):
            worker(self.store)
        error = self.store.get('worker_last_error')
        self.assertEqual((error['method'], error['rpc_code']), ('account/rateLimits/read', -32602))

    def test_worker_records_fatal_exit_and_releases_lease(self):
        with patch('engine.tick', side_effect=KeyboardInterrupt()), self.assertRaises(KeyboardInterrupt):
            worker(self.store)
        self.assertEqual(self.store.get('worker_last_exit')['reason'], 'fatal_error')
        self.assertTrue(self.store.lease('replacement', int(__import__('time').time())))

    def test_launch_does_not_misreport_dead_process_as_already_running(self):
        self.store.put('worker', {'pid': 123, 'heartbeat': NOW, 'state': 'checking'})
        with patch('server.time.time', return_value=NOW), patch('server.process_alive', return_value=False), patch('server.subprocess.Popen') as popen:
            popen.return_value.pid = 456
            self.assertEqual(server.launch(self.store), {'started_pid': 456})
        self.assertEqual(self.store.get('worker_last_launch')['worker_pid'], 456)

    def test_launch_failure_is_logged_and_raised(self):
        with patch('server.subprocess.Popen', side_effect=OSError('private')), self.assertRaises(OSError):
            server.launch(self.store)
        self.assertEqual(self.store.get('worker_last_error')['stage'], 'launch')

    def test_rpc_rejection_does_not_expose_upstream_body(self):
        rpc = Rpc(self.store.root)
        rpc.send = Mock()
        rpc.receive = Mock(return_value={'id': 1, 'error': {'code': -32602, 'message': 'Bearer private'}})
        with self.assertRaises(MetadataError) as error:
            rpc.call('account/read')
        self.assertNotIn('private', str(error.exception))
        self.assertEqual(error.exception.rpc_code, -32602)


if __name__ == '__main__':
    unittest.main()
