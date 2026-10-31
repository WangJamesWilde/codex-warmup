import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from collaboration import BridgeError, RecoveryBridge, clean_result, configure, receipt
from engine import Store
from resume import dispatch, rules, save_rule, set_rule_enabled
from test_plugin import NOW, snapshot

THREAD = '019e3130-0000-7000-8000-000000000002'
MATCH = {'status': 'collaboration', 'recoveryHandle': 'match-1', 'discussionId': 'discussion-1',
         'requestId': 9, 'deliveryId': 'delivery-1'}


class BridgeTransport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent / '.state')
        self.store = Store(self.temp.name)
        self.store.put('recovery_bridge', {'cli': str(Path(self.temp.name) / 'recovery-cli.mjs'),
                                           'node': str(Path(sys.executable))})
        self.bridge = RecoveryBridge(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, value=None, code=0, action='resolve', raw=None):
        with patch('collaboration.Path.is_file', return_value=True), patch('collaboration.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], code, raw if raw is not None else json.dumps(value), '')
            result = self.bridge.call(action, threadId=THREAD, failedTurnId='quota-turn-1')
        return result, run.call_args

    def test_resolve_has_only_explicit_control_fields(self):
        value, args = self.call({**MATCH, 'token': 'must-not-copy', 'rawOutput': 'private'})
        self.assertEqual(value, MATCH)
        self.assertEqual(args.args[0], [self.bridge.config['node'], self.bridge.config['cli']])
        self.assertFalse(args.kwargs['shell'])
        self.assertEqual(json.loads(args.kwargs['input']), {'protocolVersion': 1, 'action': 'resolve',
                         'threadId': THREAD, 'failedTurnId': 'quota-turn-1'})
        self.assertNotIn('must-not-copy', str(args))

    def test_nonzero_preserves_structured_denial_and_never_becomes_ordinary(self):
        with self.assertRaises(BridgeError) as error:
            self.call({'status': 'blocked', 'reason': 'discussion_expired', 'error': 'DISCUSSION_EXPIRED'}, code=1)
        self.assertEqual(error.exception.code, 'DISCUSSION_EXPIRED')
        self.assertEqual(error.exception.result['status'], 'blocked')

    def test_invalid_or_noisy_stdout_fails_closed(self):
        for raw in ('', '{}', 'null', '[]', '{"status":"ordinary"}\n{"token":"private"}', 'log\n{}', 'x' * 65537):
            with self.subTest(raw=raw[:30]), self.assertRaises(BridgeError):
                self.call(raw=raw)

    def test_match_must_be_complete_and_identity_must_agree(self):
        for value in ({'status': 'collaboration'}, {**MATCH, 'requestId': True},
                      {'status': 'ordinary', 'threadId': 'different-thread'},
                      {'status': 'ordinary', 'failedTurnId': 'different-turn'}):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                self.call(value)

    def test_queued_requires_native_submission_receipt(self):
        with self.assertRaises(BridgeError):
            self.call({'status': 'queued'}, action='resume')
        result, _ = self.call({'status': 'queued', 'submissionId': 'submission-1'}, action='resume')
        self.assertFalse(receipt(result)['execution_confirmed'])

    def test_started_and_completed_require_native_turn_identity(self):
        for status in ('processing', 'completed'):
            with self.subTest(status=status), self.assertRaises(BridgeError):
                self.call({'status': status}, action='status')

    def test_error_field_even_with_zero_exit_cannot_authorize_fallback(self):
        with self.assertRaises(BridgeError):
            self.call({'status': 'ordinary', 'error': 'BRIDGE_FAILED'})

    def test_status_unknown_is_not_ordinary_or_resend_permission(self):
        value, _ = self.call({'status': 'unknown'}, action='status')
        self.assertTrue(receipt(value)['bridge_pending'])
        with self.assertRaises(BridgeError):
            self.call({'status': 'ordinary'}, action='status')

    def test_real_bridge_not_found_is_unknown_and_never_resend_permission(self):
        value, _ = self.call({'status': 'not_found', 'attemptId': 'missing'}, action='status')
        self.assertEqual(receipt(value)['state'], 'bridge_unknown')
        self.assertEqual(receipt(value)['bridge_result']['status'], 'not_found')
        self.assertTrue(receipt(value)['bridge_pending'])

    def test_real_cli_failure_reason_is_preserved(self):
        with self.assertRaises(BridgeError) as error:
            self.call({'status': 'unavailable', 'reason': 'RECOVERY_UNAVAILABLE_NO_FALLBACK'}, code=1)
        self.assertEqual(error.exception.code, 'RECOVERY_UNAVAILABLE_NO_FALLBACK')

    def test_missing_or_invalid_configuration_never_launches_node(self):
        for config in (None, {}, {'cli': 1, 'node': None}, {'cli': 'relative.mjs', 'node': sys.executable}):
            self.store.put('recovery_bridge', config)
            with patch('collaboration.subprocess.run') as run, self.assertRaises(BridgeError):
                RecoveryBridge(self.store).resolve(THREAD, 'quota-turn-1')
            run.assert_not_called()

    def test_timeout_remains_ambiguous_without_raw_output(self):
        with patch('collaboration.Path.is_file', return_value=True), patch('collaboration.subprocess.run',
                side_effect=subprocess.TimeoutExpired('node', 45, output='private')):
            with self.assertRaises(BridgeError) as error:
                self.bridge.resolve(THREAD, 'quota-turn-1')
        self.assertEqual(error.exception.code, 'BRIDGE_TIMEOUT')
        self.assertNotIn('private', str(error.exception))

    def test_node_preload_environment_is_removed(self):
        with patch('collaboration.environment', return_value={'NODE_OPTIONS': 'private-preload', 'NODE_PATH': 'private-module'}):
            _, args = self.call({'status': 'ordinary'})
        self.assertNotIn('NODE_OPTIONS', args.kwargs['env'])
        self.assertNotIn('NODE_PATH', args.kwargs['env'])

    def test_configure_requires_existing_absolute_paths_and_never_sends(self):
        with self.assertRaises(ValueError):
            configure(self.store, 'relative.mjs')
        with patch('collaboration.Path.is_file', return_value=True):
            result = configure(self.store, str(Path(self.temp.name) / 'recovery-cli.mjs'), sys.executable)
        self.assertFalse(result['message_sent'])
        self.assertEqual(self.store.get('recovery_bridge')['node'], str(Path(sys.executable).resolve()))

    def test_resume_uses_milliseconds_exact_message_and_no_model_override(self):
        rule = {'id': 'rule-1', 'thread_id': THREAD, 'revision': 3, 'message': '先检查再继续；不扩权'}
        with patch.object(self.bridge, 'call', return_value={'status': 'accepted'}) as call:
            self.bridge.resume(rule, 'quota-turn-1', 'attempt-1', NOW, MATCH)
        self.assertEqual(call.call_args.args, ('resume',))
        self.assertEqual(call.call_args.kwargs['recoveryAt'], NOW * 1000)
        self.assertEqual(call.call_args.kwargs['message'], rule['message'])
        self.assertNotIn('model', call.call_args.kwargs)


class BridgeDispatch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent / '.state')
        self.store = Store(self.temp.name)
        self.store.put('enabled', True)
        self.store.put('bound_account', 'test-account')
        self.config = {'cli': 'fixture-bridge', 'node': 'fixture-node'}
        self.store.put('recovery_bridge', self.config)
        self.store.put('resume_recovery', {'at': NOW - 50, 'account': 'test-account'})
        self.rpc = Mock()
        self.rpc.snapshot.return_value = snapshot(50)
        self.thread = {'id': THREAD, 'name': '协作测试', 'model': 'original-model', 'status': {'type': 'idle'}}
        self.turn = {'id': 'quota-turn-1', 'status': 'failed', 'completedAt': NOW - 1000,
                     'error': {'codexErrorInfo': 'usageLimitExceeded'}}
        self.rpc.call.side_effect = lambda method, params: ({'thread': self.thread} if method == 'thread/read' else {'data': [self.turn]})
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=self.rpc)
        factory.return_value.__exit__ = Mock(return_value=False)
        with patch('resume.time.time', return_value=NOW - 100):
            save_rule(self.store, {'thread_id': THREAD, 'message': '保留自定义正文', 'confirm_send': True}, factory)
        self.bridge = Mock(config=self.config, fingerprint='fixture-fingerprint')
        self.bridge.resolve.return_value = MATCH
        self.bridge.resume.return_value = {'status': 'accepted'}
        self.bridge.status.return_value = {'status': 'unknown'}
        self.sender = Mock(return_value={'state': 'queued', 'queued': True, 'execution_confirmed': False})

    def tearDown(self):
        self.temp.cleanup()

    def send(self, now=NOW):
        dispatch(self.store, self.rpc, snapshot(50, at=now), now, self.sender, lambda: True, lambda _: self.bridge)

    def attempts(self):
        with self.store.db() as db:
            return db.execute('SELECT result FROM resume_attempts').fetchall()

    def test_collaboration_goes_only_through_bridge_and_is_durable_before_send(self):
        def resumed(rule, turn, attempt, recovery, route):
            ledger = json.loads(self.attempts()[0][0])
            self.assertEqual(ledger['attempt_id'], attempt)
            self.assertFalse(rules(self.store)[0]['enabled'])
            self.assertEqual(rule['message'], '保留自定义正文')
            return {'status': 'accepted'}
        self.bridge.resume.side_effect = resumed
        self.send(); self.send()
        self.bridge.resume.assert_called_once()
        self.sender.assert_not_called()
        result = rules(self.store)[0]['last_result']
        self.assertEqual(result['state'], 'bridge_accepted')
        self.assertFalse(result['queued'])

    def test_only_explicit_ordinary_takes_original_transport(self):
        self.bridge.resolve.return_value = {'status': 'ordinary'}
        self.send()
        self.sender.assert_called_once_with(THREAD, '保留自定义正文')
        self.bridge.resume.assert_not_called()

    def test_resolve_denial_and_error_do_not_consume_count_or_send(self):
        for status in ('blocked', 'ambiguous', 'error'):
            self.bridge.resolve.side_effect = BridgeError('BRIDGE_TIMEOUT') if status == 'error' else None
            self.bridge.resolve.return_value = {'status': status}
            self.send()
            rule = rules(self.store)[0]
            self.assertTrue(rule['enabled'])
            self.assertEqual(rule['remaining_runs'], 1)
            self.assertEqual(self.attempts(), [])
        self.sender.assert_not_called()
        self.bridge.resume.assert_not_called()

    def test_timeout_after_resume_keeps_attempt_id_and_never_falls_back(self):
        self.bridge.resume.side_effect = BridgeError('BRIDGE_TIMEOUT')
        self.send(); self.send(now=NOW + 61)
        self.bridge.resume.assert_called_once()
        self.bridge.status.assert_called_once_with(json.loads(self.attempts()[0][0])['attempt_id'])
        self.sender.assert_not_called()
        self.assertEqual(rules(self.store)[0]['remaining_runs'], 0)

    def test_pending_attempt_reconciles_even_when_single_rule_disabled(self):
        self.send()
        self.bridge.status.return_value = {'status': 'queued', 'submissionId': 'submission-1'}
        self.send(now=NOW + 61)
        result = rules(self.store)[0]['last_result']
        self.assertEqual(result['state'], 'bridge_queued')
        self.assertTrue(result['queued'])
        self.assertFalse(result['execution_confirmed'])
        self.bridge.status.return_value = {'status': 'completed', 'submissionId': 'submission-1', 'resumedTurnId': 'resumed-turn-1'}
        self.send(now=NOW + 122)
        self.assertTrue(rules(self.store)[0]['last_result']['execution_confirmed'])
        self.assertFalse(json.loads(self.attempts()[0][0])['bridge_pending'])
        self.bridge.resume.assert_called_once()

    def test_status_error_preserves_last_confirmed_state_without_retry(self):
        self.send()
        self.bridge.status.side_effect = BridgeError('BRIDGE_UNAVAILABLE')
        self.send(now=NOW + 61); self.send(now=NOW + 62)
        self.assertEqual(rules(self.store)[0]['last_result']['state'], 'bridge_accepted')
        self.bridge.status.assert_called_once()
        self.bridge.resume.assert_called_once()

    def test_new_account_or_different_bridge_never_queries_old_receipt(self):
        self.send()
        self.store.put('bound_account', 'new-account')
        self.send(now=NOW + 61)
        self.store.put('bound_account', 'test-account')
        self.bridge.fingerprint = 'different-bridge'
        self.send(now=NOW + 122)
        self.bridge.status.assert_not_called()

    def test_cancel_during_resolve_prevents_reservation_and_send(self):
        def resolve(*_):
            set_rule_enabled(self.store, {'thread_id': THREAD, 'enabled': False, 'expected_revision': 1})
            return MATCH
        self.bridge.resolve.side_effect = resolve
        self.send()
        self.assertEqual(self.attempts(), [])
        self.bridge.resume.assert_not_called()

    def test_cancel_after_reservation_prevents_bridge_commit(self):
        def snapshot_before_send():
            set_rule_enabled(self.store, {'thread_id': THREAD, 'enabled': False, 'expected_revision': 1})
            return snapshot(50)
        self.rpc.snapshot.side_effect = snapshot_before_send
        self.send()
        self.bridge.resume.assert_not_called()
        self.sender.assert_not_called()

    def test_bridge_config_changed_before_commit_prevents_send(self):
        def snapshot_before_send():
            self.store.put('recovery_bridge', {'cli': 'changed'})
            return snapshot(50)
        self.rpc.snapshot.side_effect = snapshot_before_send
        self.send()
        self.bridge.resume.assert_not_called()

    def test_concurrent_workers_reserve_only_one_bridge_attempt(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.send(), range(8)))
        self.bridge.resume.assert_called_once()
        self.assertEqual(len(self.attempts()), 1)

    def test_latest_turn_change_prevents_bridge_commit(self):
        def resolve(*_):
            self.turn['id'] = 'newer-turn'
            return MATCH
        self.bridge.resolve.side_effect = resolve
        self.send()
        self.bridge.resume.assert_not_called()

    def test_pending_receipt_remains_queryable_after_days_without_resending(self):
        self.send(); self.send(now=NOW + 86401)
        self.bridge.status.assert_called_once()
        self.assertTrue(json.loads(self.attempts()[0][0])['bridge_pending'])
        self.bridge.status.return_value = {'status': 'completed', 'submissionId': 'late-submission', 'resumedTurnId': 'late-turn'}
        self.send(now=NOW + 7 * 86400)
        self.assertFalse(json.loads(self.attempts()[0][0])['bridge_pending'])
        self.assertEqual(rules(self.store)[0]['last_result']['state'], 'bridge_completed')
        self.assertEqual(rules(self.store)[0]['remaining_runs'], 0)
        self.bridge.resume.assert_called_once()

    def test_unconfigured_install_keeps_original_behavior(self):
        self.store.put('recovery_bridge', None)
        self.send()
        self.sender.assert_called_once_with(THREAD, '保留自定义正文')
        self.bridge.resolve.assert_not_called()

    def test_corrupt_config_is_not_treated_as_unconfigured(self):
        self.store.put('recovery_bridge', {})
        dispatch(self.store, self.rpc, snapshot(50), NOW, self.sender, lambda: True)
        self.sender.assert_not_called()
        self.assertEqual(rules(self.store)[0]['remaining_runs'], 1)
        self.assertEqual(rules(self.store)[0]['last_result']['state'], 'bridge_unavailable')


if __name__ == '__main__':
    unittest.main()
