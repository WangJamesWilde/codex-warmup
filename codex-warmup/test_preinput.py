import json
import unittest
from unittest.mock import Mock, patch

from collaboration import BridgeError, RecoveryBridge
from resume import dispatch, rules, save_rule, set_rule_enabled
import test_resume
from test_plugin import NOW, snapshot

THREAD = test_resume.THREAD


class Preinput(unittest.TestCase):
    setUp = test_resume.Resume.setUp
    tearDown = test_resume.Resume.tearDown

    def save(self, **extra):
        return test_resume.Resume.save(self, trigger='after_recovery', **extra)

    def send(self, **extra):
        dispatch(self.store, self.rpc, snapshot(50), NOW, self.sender, lambda: True, **extra)

    def completed(self):
        self.turn.update(status='completed', error=None)

    def test_explicit_preinput_sends_after_normal_completion_without_fake_quota(self):
        self.save(); self.completed(); self.send(); self.send()
        self.sender.assert_called_once_with(THREAD, '继续')
        self.assertEqual(self.turn['status'], 'completed')
        self.assertIsNone(self.turn['error'])
        rule = rules(self.store)[0]
        self.assertEqual(rule['trigger'], 'after_recovery')
        self.assertEqual(rule['remaining_runs'], 0)

    def test_old_quota_only_rule_does_not_gain_new_permission(self):
        test_resume.Resume.save(self)
        self.completed(); self.send()
        self.sender.assert_not_called()
        self.assertEqual(rules(self.store)[0]['trigger'], 'quota_stop')
        self.assertEqual(rules(self.store)[0]['remaining_runs'], 1)

    def test_new_manual_activity_invalidates_single_preinput_snapshot(self):
        self.save(); self.completed(); self.turn['id'] = 'new-manual-work'; self.send()
        self.sender.assert_not_called()
        rule = rules(self.store)[0]
        self.assertEqual(self.store.get('resume_diagnostics')[rule['id']]['reason'], 'preinput_anchor_changed')
        self.assertEqual(rule['remaining_runs'], 1)

    def test_task_that_kept_running_beyond_reset_is_not_restarted(self):
        self.save(); self.completed(); self.turn['completedAt'] = NOW; self.send()
        self.sender.assert_not_called()
        self.assertEqual(rules(self.store)[0]['remaining_runs'], 1)

    def test_busy_approval_input_wait_and_archived_are_not_sent(self):
        self.save(); self.completed()
        for status in ({'type':'active'}, {'type':'idle','activeFlags':['waitingOnApproval']},
                       {'type':'idle','activeFlags':['waitingOnUserInput']}):
            self.thread['status'] = status
            self.send()
        self.thread['status'] = {'type':'idle'}
        self.thread['archived'] = True
        self.send()
        self.sender.assert_not_called()

    def test_manual_interruption_and_nonquota_errors_are_not_normal_completion(self):
        self.save()
        for status, error in [('interrupted', None), ('failed', {'codexErrorInfo':'rateLimitExceeded'}),
                              ('completed', {'codexErrorInfo':'other'})]:
            self.turn.update(status=status, error=error)
            self.send()
        self.sender.assert_not_called()

    def test_explicit_preinput_also_supports_real_quota_stop(self):
        self.save(); self.send()
        self.sender.assert_called_once()

    def test_cannot_send_without_observed_recovery(self):
        self.save(); self.completed(); self.store.put('resume_recovery', None); self.send()
        self.sender.assert_not_called()

    def test_repeat_is_at_most_once_per_recovery_even_if_turn_changes(self):
        self.save(run_limit=3); self.completed(); self.send()
        self.turn['id'] = 'another-old-completed-turn'; self.send()
        self.assertEqual(self.sender.call_count, 1)
        self.store.put('resume_recovery', {'at':NOW - 40, 'account':'test-account'})
        self.send()
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(rules(self.store)[0]['remaining_runs'], 1)

    def test_cancellation_prevents_send_without_losing_message_or_trigger(self):
        self.save(message='Resume the saved plan')
        set_rule_enabled(self.store, {'thread_id':THREAD,'enabled':False,'expected_revision':1}, self.factory)
        self.completed(); self.send()
        self.sender.assert_not_called()
        self.assertEqual(rules(self.store)[0]['trigger'], 'after_recovery')
        self.assertEqual(rules(self.store)[0]['message'], 'Resume the saved plan')

    def test_rearm_captures_new_anchor_and_still_waits_for_next_recovery(self):
        self.save(); set_rule_enabled(self.store, {'thread_id':THREAD,'enabled':False,'expected_revision':1}, self.factory)
        self.turn['id'] = 'new-selected-turn'
        with patch('resume.time.time', return_value=NOW):
            set_rule_enabled(self.store, {'thread_id':THREAD,'enabled':True,'expected_revision':2,'confirm_send':True}, self.factory)
        self.assertEqual(rules(self.store)[0]['anchor_turn_id'], 'new-selected-turn')
        self.completed(); self.send()
        self.sender.assert_not_called()

    def test_paused_or_complete_goal_is_not_overridden(self):
        self.save(); self.completed()
        for state in ('paused','blocked','complete','budgetLimited'):
            self.rpc.call.side_effect = lambda method, params: ({'thread':self.thread} if method=='thread/read'
                else {'goal':{'status':state}} if method=='thread/goal/get' else {'data':[self.turn]})
            self.send()
        self.sender.assert_not_called()

    def test_old_bridge_blocks_normal_completion_without_count_or_plain_fallback(self):
        self.save(); self.completed(); self.store.put('recovery_bridge', {'fixture':True})
        bridge = Mock()
        bridge.resolve_preinput.side_effect = BridgeError('INVALID_RECOVERY_REQUEST')
        self.send(bridge_factory=lambda _:bridge)
        self.sender.assert_not_called(); bridge.resolve.assert_not_called(); bridge.resume.assert_not_called()
        self.assertEqual(rules(self.store)[0]['remaining_runs'], 1)

    def test_bridge_must_explicitly_classify_completed_turn_as_private_ordinary(self):
        self.save(); self.completed(); self.store.put('recovery_bridge', {'fixture':True})
        bridge = Mock()
        bridge.config = self.store.get('recovery_bridge')
        bridge.resolve_preinput.return_value = {'status':'ordinary'}
        self.send(bridge_factory=lambda _:bridge)
        self.sender.assert_called_once()
        bridge.resolve.assert_not_called(); bridge.resume.assert_not_called()

    def test_ambiguous_bridge_does_not_fall_back(self):
        self.save(); self.completed(); self.store.put('recovery_bridge', {'fixture':True})
        bridge = Mock()
        bridge.resolve_preinput.return_value = {'status':'ambiguous'}
        self.send(bridge_factory=lambda _:bridge)
        self.sender.assert_not_called()
        self.assertEqual(rules(self.store)[0]['remaining_runs'], 1)

    def test_missing_anchor_or_invalid_trigger_cannot_create_intent(self):
        self.turn['id'] = None
        with self.assertRaises(ValueError): self.save()
        with self.assertRaises(ValueError): test_resume.Resume.save(self, trigger='anything')
        self.assertEqual(rules(self.store), [])

    def test_bridge_classifier_cannot_omit_or_change_target_or_grant_group_recovery(self):
        bridge=RecoveryBridge(self.store)
        self.store.put('recovery_bridge', {'cli':__file__,'node':__file__})
        bridge=RecoveryBridge(self.store)
        for response in ({'status':'ordinary'}, {'status':'ordinary','threadId':THREAD,'completedTurnId':'wrong'},
                         {'status':'collaboration','threadId':THREAD,'completedTurnId':self.turn['id']}):
            with patch('collaboration.Path.is_file',return_value=True),patch('collaboration.subprocess.run') as run:
                run.return_value.returncode=0;run.return_value.stdout=json.dumps(response)
                with self.assertRaises(BridgeError):bridge.resolve_preinput(THREAD,self.turn['id'])


if __name__ == '__main__':
    unittest.main()
