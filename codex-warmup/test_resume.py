import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from engine import Store
from resume import save_rule, rules, dispatch, quota_stop, observed_recovery, set_rule_enabled, set_rule_mode
from runtime import queue_message
from test_plugin import NOW, snapshot

THREAD = '019e3130-0000-7000-8000-000000000001'


class Resume(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent / '.state')
        self.store = Store(self.temp.name)
        self.store.put('enabled', True)
        self.store.put('bound_account', 'test-account')
        self.rpc = Mock()
        self.rpc.snapshot.return_value = snapshot(50)
        self.thread = {'id': THREAD, 'name': '测试对话', 'model': 'original-model',
                       'reasoningEffort': 'xhigh', 'status': {'type': 'idle'}}
        self.turn = {'id': 'quota-turn-one', 'status': 'failed', 'completedAt': NOW - 1000,
                     'error': {'codexErrorInfo': 'usageLimitExceeded'}}
        self.rpc.call.side_effect = lambda method, params: ({'thread': self.thread} if method == 'thread/read' else {'data': [self.turn]})
        self.factory = Mock()
        self.factory.return_value.__enter__ = Mock(return_value=self.rpc)
        self.factory.return_value.__exit__ = Mock(return_value=False)
        self.sender = Mock(return_value={'queued': True, 'state': 'queued', 'execution_confirmed': False})
        self.store.put('resume_recovery', {'at': NOW - 50, 'reset': 37000, 'account': 'test-account'})

    def tearDown(self):
        self.temp.cleanup()

    def save(self, **extra):
        with patch('resume.time.time', return_value=NOW - 100):
            return save_rule(self.store, {'thread_id': THREAD, 'message': '继续', 'confirm_send': True, **extra}, self.factory)

    def send(self, snap=None, now=NOW, running=True):
        dispatch(self.store, self.rpc, snap or snapshot(50), now, self.sender, lambda: running)

    def test_save_requires_specific_consent_and_does_not_send(self):
        with self.assertRaises(ValueError):
            save_rule(self.store, {'thread_id': THREAD, 'message': '继续'}, self.factory)
        self.save()
        self.sender.assert_not_called()
        self.assertFalse(rules(self.store)[0]['repeat'])

    def test_new_rule_waits_for_next_not_existing_recovery(self):
        with patch('resume.time.time', return_value=NOW):
            save_rule(self.store, {'thread_id': THREAD, 'message': '继续', 'confirm_send': True}, self.factory)
        self.send()
        self.sender.assert_not_called()

    def test_single_use_disarms_and_is_not_resent(self):
        self.save(); self.send(); self.send()
        self.sender.assert_called_once_with(THREAD, '继续')
        self.assertFalse(rules(self.store)[0]['enabled'])
        self.assertFalse(rules(self.store)[0]['last_result']['execution_confirmed'])

    def test_repeating_rule_needs_a_new_quota_stopped_turn(self):
        self.save(repeat=True); self.send(); self.send()
        self.assertTrue(rules(self.store)[0]['enabled'])
        self.assertEqual(self.sender.call_count, 1)
        self.store.put('resume_recovery', {'at': NOW, 'account': 'test-account'})
        self.turn['id'] = 'quota-turn-two'
        self.send()
        self.assertEqual(self.sender.call_count, 2)

    def test_eight_claimers_only_one_queue_submission(self):
        self.save()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.send(), range(8)))
        self.sender.assert_called_once()

    def test_timeout_is_not_retried_even_for_repeat(self):
        self.save(repeat=True)
        self.sender.side_effect = TimeoutError()
        self.send(); self.send()
        self.sender.assert_called_once()
        self.assertEqual(rules(self.store)[0]['last_result']['state'], 'unconfirmed_no_retry')

    def test_closed_desktop_waits_without_launching_or_sending(self):
        self.save(); self.send(running=False)
        self.sender.assert_not_called()
        self.assertTrue(rules(self.store)[0]['enabled'])

    def test_blocked_weekly_and_other_account_do_not_send(self):
        self.save()
        self.send(snapshot(50, weekly=100))
        self.send(snapshot(50, account='someone-else'))
        self.sender.assert_not_called()

    def test_running_waiting_approval_and_unknown_state_do_not_send(self):
        self.save()
        for status in ({'type':'active','activeFlags':[]}, {'type':'active','activeFlags':['waitingOnApproval']}, {'type':'systemError'}):
            self.thread['status'] = status
            self.send()
        self.sender.assert_not_called()

    def test_explicitly_paused_or_budget_limited_goal_is_not_resumed(self):
        self.save()
        for goal_status in ('paused', 'blocked', 'complete', 'budgetLimited'):
            self.rpc.call.side_effect = lambda method, params: ({'thread': self.thread} if method == 'thread/read' else
                {'goal': {'status': goal_status}} if method == 'thread/goal/get' else {'data': [self.turn]})
            self.send()
        self.sender.assert_not_called()

    def test_account_switch_immediately_before_queue_cancels_send(self):
        self.save()
        self.rpc.snapshot.return_value = snapshot(50, account='changed-before-send')
        self.send()
        self.sender.assert_not_called()

    def test_manual_pause_and_error_prose_are_not_quota_evidence(self):
        self.save()
        for error in (None, {'message':'usage limit exceeded'}, {'codexErrorInfo':'rateLimitExceeded'}):
            self.turn['status']='interrupted'; self.turn['error']=error
            self.send()
        self.sender.assert_not_called()

    def test_incomplete_or_later_turn_cannot_be_recovered(self):
        self.save()
        for completed in (None, NOW):
            self.turn['completedAt'] = completed
            self.send()
        self.sender.assert_not_called()

    def test_recovery_is_observed_even_if_other_chat_already_used_quota(self):
        recovery = observed_recovery(snapshot(5, reset=37000), snapshot(100, reset=19000), 19000, NOW)
        self.assertEqual(recovery['at'], NOW)
        self.assertIsNone(observed_recovery(snapshot(5), snapshot(5), 37000, NOW))

    def test_corrupt_previous_snapshot_does_not_block_future_checks(self):
        previous=snapshot(100)
        previous['primary']['usedPercent']=None
        self.assertIsNone(observed_recovery(snapshot(), previous, 19000, NOW))
        from engine import choose_cycle
        self.assertIsNotNone(choose_cycle(snapshot(), previous, 19000, NOW)[0])

    def test_queue_transport_never_overrides_model_or_approvals(self):
        with patch('runtime.executable', return_value=Path('codex.exe')), patch('runtime.subprocess.run') as run:
            run.return_value.returncode=0; run.return_value.stdout='Queued message example for thread example.'
            self.assertTrue(queue_message(THREAD, '自定义消息')['queued'])
        args=run.call_args.args[0]
        self.assertEqual(args[1:], ['queue','--thread',THREAD,'--message','自定义消息'])
        self.assertNotIn('--model', args)

    def test_tick_dispatches_recovery_even_when_another_chat_used_window(self):
        self.save()
        self.store.put('snapshot',snapshot(100,reset=19000))
        self.store.put('next_reset',19000)
        from engine import tick
        with patch('resume.queue_message',self.sender),patch('resume.desktop_running',return_value=True):
            self.assertEqual(tick(self.store,self.factory,NOW),'window_already_active')
        self.sender.assert_called_once_with(THREAD,'继续')
        self.rpc.warmup.assert_not_called()

    def test_resume_after_warmup_uses_fresh_clock_and_snapshot(self):
        self.save()
        self.store.put('snapshot',snapshot(100,reset=19000))
        self.store.put('next_reset',19000)
        self.rpc.snapshot.side_effect=[snapshot(),snapshot(1,at=NOW+3),snapshot(1,at=NOW+3)]
        from engine import tick
        with patch('engine.time.time',return_value=NOW) as clock,patch('resume.queue_message',self.sender),patch('resume.desktop_running',return_value=True):
            def warmed():
                clock.return_value=NOW+3
                return {'request_completed':True}
            self.rpc.warmup.side_effect=warmed
            tick(self.store,self.factory)
        self.rpc.warmup.assert_called_once()
        self.sender.assert_called_once_with(THREAD,'继续')

    def quick(self, enabled=True, revision=0, **extra):
        return set_rule_enabled(self.store, {'thread_id': THREAD, 'enabled': enabled,
            'expected_revision': revision, **({'confirm_send': True} if enabled else {}), **extra}, self.factory)

    def test_quick_first_click_defaults_to_single_continue_without_sending(self):
        self.quick()
        rule=rules(self.store)[0]
        self.assertEqual((rule['message'],rule['repeat'],rule['enabled']),('继续',False,True))
        self.sender.assert_not_called()

    def test_quick_enable_requires_consent(self):
        with self.assertRaises(ValueError):
            self.quick(confirm_send=False)
        self.assertEqual(rules(self.store),[])

    def test_quick_cancel_is_offline_and_preserves_custom_message_and_repeat(self):
        self.save(message='先检查再继续',repeat=True)
        self.factory.reset_mock()
        self.quick(False,1)
        self.factory.assert_not_called()
        rule=rules(self.store)[0]
        self.assertEqual((rule['message'],rule['repeat'],rule['enabled']),('先检查再继续',True,False))
        self.quick(True,2)
        rule=rules(self.store)[0]
        self.assertEqual((rule['message'],rule['repeat'],rule['enabled']),('先检查再继续',True,True))

    def test_quick_stale_revision_never_overwrites_later_edit(self):
        self.save(message='新消息')
        result=self.quick(False,0)
        self.assertTrue(result['conflict'])
        self.assertTrue(rules(self.store)[0]['enabled'])
        self.assertEqual(rules(self.store)[0]['message'],'新消息')

    def test_quick_two_enabled_requests_do_not_move_the_next_recovery_boundary(self):
        with patch('resume.time.time',return_value=NOW):
            self.quick()
        with patch('resume.time.time',return_value=NOW+20):
            self.quick(True,1)
        self.assertEqual(rules(self.store)[0]['armed_at'],NOW)
        self.assertEqual(rules(self.store)[0]['revision'],1)

    def test_quick_rearm_waits_for_next_recovery(self):
        self.save()
        self.quick(False,1)
        with patch('resume.time.time',return_value=NOW):
            self.quick(True,2)
        self.send()
        self.sender.assert_not_called()

    def test_eight_quick_add_requests_create_one_rule(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.quick(),range(8)))
        self.assertEqual(sum(not row['conflict'] for row in results),1)
        self.assertEqual(len(rules(self.store)),1)

    def test_quick_cancel_after_send_reservation_still_prevents_queue(self):
        self.save()
        def before_queue():
            self.quick(False,1)
            return snapshot(50)
        self.rpc.snapshot.side_effect=before_queue
        self.send()
        self.sender.assert_not_called()
        self.assertFalse(rules(self.store)[0]['enabled'])

    def test_quick_cancel_does_not_erase_previously_queued_result(self):
        self.save();self.send()
        self.quick(False,1)
        self.assertEqual(rules(self.store)[0]['last_result']['state'],'queued')
        self.sender.assert_called_once()

    def test_quick_cannot_reenable_for_changed_account(self):
        self.save();self.quick(False,1)
        self.rpc.snapshot.return_value=snapshot(50,account='different-account')
        with self.assertRaises(ValueError):
            self.quick(True,2)
        self.assertFalse(rules(self.store)[0]['enabled'])

    def test_selected_count_stops_after_n_distinct_recoveries(self):
        self.save(run_limit=3)
        for number in range(3):
            self.turn['id']='quota-stop-'+str(number)
            self.store.put('resume_recovery',{'at':NOW-50+number,'account':'test-account'})
            self.send();self.send()
            rule=rules(self.store)[0]
            self.assertEqual(rule['remaining_runs'],2-number)
            self.assertEqual(rule['enabled'],number<2)
        self.assertEqual(self.sender.call_count,3)

    def test_count_cancel_and_resume_preserves_remaining_runs(self):
        self.save(run_limit=3);self.send()
        self.quick(False,1);self.quick(True,2)
        self.assertEqual(rules(self.store)[0]['remaining_runs'],2)

    def test_editing_message_does_not_silently_reset_count(self):
        self.save(run_limit=3);self.send()
        self.save(run_limit=3,message='改成新消息')
        self.assertEqual(rules(self.store)[0]['remaining_runs'],2)

    def test_mode_change_preserves_paused_state_and_waits_for_next_recovery(self):
        self.save();self.quick(False,1)
        rule=rules(self.store)[0]
        set_rule_mode(self.store,{'rule_id':rule['id'],'expected_revision':2,'run_limit':4,'confirm_send':True})
        rule=rules(self.store)[0]
        self.assertFalse(rule['enabled'])
        self.assertEqual((rule['run_limit'],rule['remaining_runs']),(4,4))
        self.sender.assert_not_called()

    def test_mode_rejects_invalid_count_and_stale_revision(self):
        self.save();rule=rules(self.store)[0]
        for value in (0,-1,1001,1.5,True,'3'):
            with self.assertRaises(ValueError):
                set_rule_mode(self.store,{'rule_id':rule['id'],'expected_revision':1,'run_limit':value,'confirm_send':True})
        result=set_rule_mode(self.store,{'rule_id':rule['id'],'expected_revision':2,'run_limit':3,'confirm_send':True})
        self.assertTrue(result['conflict'])
        self.assertEqual(rules(self.store)[0]['run_limit'],1)

    def test_legacy_rules_migrate_without_losing_message_or_mode(self):
        with self.store.db() as db:
            db.execute('CREATE TABLE resume_rules (id TEXT PRIMARY KEY,account TEXT,thread_id TEXT,title TEXT,message TEXT,repeat INTEGER,enabled INTEGER,armed_at INTEGER,revision INTEGER,last_result TEXT,UNIQUE(account,thread_id))')
            db.execute('INSERT INTO resume_rules VALUES (?,?,?,?,?,?,?,?,?,?)',('legacy-one','test-account',THREAD,'原规则','保留消息',0,1,NOW,1,None))
        rule=rules(self.store)[0]
        self.assertEqual((rule['message'],rule['run_limit'],rule['remaining_runs']),('保留消息',1,1))

    def test_exhausted_count_can_be_explicitly_rearmed_without_changing_limit(self):
        self.save(run_limit=2);self.send()
        self.turn['id']='second-stop';self.send()
        self.assertFalse(rules(self.store)[0]['enabled'])
        self.quick(True,1)
        rule=rules(self.store)[0]
        self.assertEqual((rule['enabled'],rule['run_limit'],rule['remaining_runs']),(True,2,2))


if __name__ == '__main__': unittest.main()
