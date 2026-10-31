import concurrent.futures
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from engine import Store
from quota_warning import configure, warning
from test_plugin import NOW, snapshot

THREAD='11111111-1111-4111-8111-111111111111'


class Warning(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent/'.state')
        self.store=Store(self.temp.name)
        self.store.put('enabled',True)
        self.store.put('bound_account','test-account')
        self.store.put('snapshot',snapshot(95))
        self.event={'session_id':THREAD,'hook_event_name':'PostToolUse','turn_id':'active-turn','model':'gpt-6.1-sol'}

    def tearDown(self):
        self.temp.cleanup()

    def emit(self):
        return warning(self.event,self.temp.name,NOW)

    def test_default_disabled_does_not_emit_or_create_rules(self):
        self.assertEqual(self.emit(),{})
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM actions').fetchone()[0],0)

    def test_threshold_five_outputs_informational_context_once_without_control_fields(self):
        configure(self.store,{'enabled':True,'threshold':5})
        result=self.emit()
        self.assertEqual(set(result),{'hookSpecificOutput'})
        self.assertEqual(set(result['hookSpecificOutput']),{'hookEventName','additionalContext'})
        self.assertIn('5%',result['hookSpecificOutput']['additionalContext'])
        self.assertIn('after_recovery',result['hookSpecificOutput']['additionalContext'])
        self.assertLess(len(result['hookSpecificOutput']['additionalContext']),500)
        self.assertEqual(self.emit(),{})

    def test_threshold_three_waits_until_three_percent(self):
        configure(self.store,{'enabled':True,'threshold':3})
        self.assertEqual(self.emit(),{})
        self.store.put('snapshot',snapshot(97))
        self.assertTrue(self.emit())

    def test_changing_threshold_does_not_repeat_within_same_cycle(self):
        configure(self.store,{'enabled':True,'threshold':5});self.emit()
        configure(self.store,{'enabled':True,'threshold':3})
        self.store.put('snapshot',snapshot(98))
        self.assertEqual(self.emit(),{})

    def test_new_cycle_and_another_chat_have_independent_once_only_claims(self):
        configure(self.store,{'enabled':True,'threshold':5});self.emit()
        self.event['session_id']='22222222-2222-4222-8222-222222222222'
        self.assertTrue(self.emit())
        self.event['session_id']=THREAD
        self.store.put('snapshot',snapshot(95,reset=38000))
        self.assertTrue(self.emit())

    def test_eight_parallel_hook_calls_emit_only_once(self):
        configure(self.store,{'enabled':True,'threshold':5})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values=list(pool.map(lambda _:self.emit(),range(8)))
        self.assertEqual(sum(bool(x) for x in values),1)

    def test_stale_unknown_blocked_expired_zero_and_changed_account_do_not_warn(self):
        configure(self.store,{'enabled':True,'threshold':5})
        values=[snapshot(95,at=NOW-121),snapshot(100),snapshot(95,account='other'),snapshot(95,reset=NOW-1),snapshot(95,weekly=100)]
        blocked=snapshot(95);blocked['blocked']=True;values.append(blocked)
        for snap in values:
            self.store.put('snapshot',snap)
            self.assertEqual(self.emit(),{})

    def test_paused_background_or_unsupported_model_does_not_warn(self):
        configure(self.store,{'enabled':True,'threshold':5})
        self.store.put('enabled',False)
        self.assertEqual(self.emit(),{})
        self.store.put('enabled',True)
        self.event['model']='not-a-codex-model'
        self.assertEqual(self.emit(),{})

    def test_requires_known_thread_and_turn_and_exact_post_tool_event(self):
        configure(self.store,{'enabled':True,'threshold':5})
        for key,value in [('session_id','invalid'),('turn_id',None),('hook_event_name','Stop')]:
            previous=self.event[key];self.event[key]=value
            self.assertEqual(self.emit(),{})
            self.event[key]=previous

    def test_known_subagent_does_not_consume_parent_warning(self):
        configure(self.store,{'enabled':True,'threshold':5})
        self.event['agent_id']='child-agent'
        self.assertEqual(self.emit(),{})
        del self.event['agent_id']
        self.assertEqual(warning(self.event,self.temp.name,NOW,caller_thread_id='different-child-thread'),{})
        self.assertTrue(self.emit())

    def test_configuration_rejects_invalid_fields(self):
        for data in ({'enabled':1,'threshold':5},{'enabled':True,'threshold':True},{'enabled':True,'threshold':9}):
            with self.assertRaises(ValueError):configure(self.store,data)

    def test_hook_command_ignores_tool_secrets_and_does_not_create_missing_state(self):
        root=Path(__file__).parent
        env=dict(os.environ,CODEX_WARMUP_HOME=str(Path(self.temp.name)/'missing'),PYTHONIOENCODING='utf-8')
        event={**self.event,'tool_input':{'password':'private-secret'},'tool_response':'private output'}
        result=subprocess.run([sys.executable,'-B',str(root/'hooks/low-quota.py')],input=json.dumps(event),
                              capture_output=True,text=True,encoding='utf-8',env=env,timeout=5)
        self.assertEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stdout),{})
        self.assertNotIn('private',result.stdout+result.stderr)
        self.assertFalse(Path(env['CODEX_WARMUP_HOME']).exists())

    def test_packaged_hook_has_no_async_turn_or_approval_mutations(self):
        spec=json.loads((Path(__file__).parent/'hooks/hooks.json').read_text(encoding='utf-8'))
        self.assertEqual(set(spec['hooks']),{'PostToolUse'})
        handler=spec['hooks']['PostToolUse'][0]['hooks'][0]
        self.assertEqual(handler['timeout'],3)
        self.assertIn('${PLUGIN_ROOT}',handler['command'])
        self.assertNotIn('async',handler)


if __name__=='__main__':unittest.main()
