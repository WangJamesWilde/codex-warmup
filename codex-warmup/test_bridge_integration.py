"""Opt in with CODEX_WARMUP_BRIDGE_SOURCE; no live bridge/model is ever used."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from collaboration import BridgeError, RecoveryBridge, configure
from engine import Store
from resume import dispatch, reconcile_bridge, rules, save_rule
from test_plugin import snapshot


@unittest.skipUnless(os.environ.get('CODEX_WARMUP_BRIDGE_SOURCE') and shutil.which('node'),
                     'Set CODEX_WARMUP_BRIDGE_SOURCE for isolated cross-project integration')
class IsolatedBridgeIntegration(unittest.TestCase):
    def test_completed_private_preinput_through_real_bridge_blocks_pending_input_then_sends_once(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent / '.state') as temporary:
            root=Path(temporary)
            proc=subprocess.Popen([shutil.which('node'),str(Path(__file__).parent/'fixtures/bridge-harness.mjs'),
                os.environ['CODEX_WARMUP_BRIDGE_SOURCE'],str(root)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            lines=queue.Queue()
            reader=threading.Thread(target=lambda:[lines.put(line) for line in proc.stdout],daemon=True);reader.start()
            def control(action):
                proc.stdin.write(json.dumps({'action':action})+'\n');proc.stdin.flush()
                return json.loads(lines.get(timeout=15))
            try:
                ready=json.loads(lines.get(timeout=15));now=int(time.time())
                store=Store(root/'plugin-state');configure(store,ready['cli'],shutil.which('node'))
                store.put('enabled',True);store.put('bound_account','test-account')
                store.put('resume_recovery',{'at':now-10,'account':'test-account'})
                rpc=Mock();rpc.snapshot.return_value=snapshot(50,at=now)
                rpc.call.side_effect=lambda method,params: (
                    {'thread':{'id':ready['privateThreadId'],'name':'Private fixture','model':'original-model','status':{'type':'idle'}}}
                    if method=='thread/read' else {'goal':None} if method=='thread/goal/get' else
                    {'data':[{'id':ready['privateTurnId'],'status':'completed','error':None,'completedAt':now-30}]})
                factory=Mock();factory.return_value.__enter__=Mock(return_value=rpc);factory.return_value.__exit__=Mock(return_value=False)
                with patch('resume.time.time',return_value=now-20):
                    save_rule(store,{'thread_id':ready['privateThreadId'],'message':'Fixture next-window input',
                        'confirm_send':True,'trigger':'after_recovery'},factory)
                sender=Mock(return_value={'queued':True,'state':'queued','execution_confirmed':False})
                self.assertEqual(control('private_queue')['recoveryAttempts'],0)
                dispatch(store,rpc,snapshot(50,at=now),now,sender,lambda:True)
                sender.assert_not_called();self.assertEqual(rules(store)[0]['remaining_runs'],1)
                self.assertEqual(rules(store)[0]['last_result']['state'],'bridge_blocked')
                control('private_idle')
                dispatch(store,rpc,snapshot(50,at=now),now,sender,lambda:True)
                dispatch(store,rpc,snapshot(50,at=now),now,sender,lambda:True)
                sender.assert_called_once_with(ready['privateThreadId'],'Fixture next-window input')
                self.assertEqual(rules(store)[0]['remaining_runs'],0)
                untouched=control('private_idle')
                self.assertEqual(untouched['calls'],1);self.assertEqual(untouched['recoveryAttempts'],0)
            finally:
                if proc.poll() is None:
                    try:proc.stdin.write('{"action":"stop"}\n');proc.stdin.flush()
                    except (OSError,BrokenPipeError):pass
                    try:proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
                proc.stdin.close();reader.join(timeout=2);proc.stdout.close()

    def test_real_cli_and_gateway_recover_after_long_wait_once_even_if_reply_lost(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent.parent / '.state') as temporary:
            root = Path(temporary)
            proc = subprocess.Popen([shutil.which('node'), str(Path(__file__).parent / 'fixtures/bridge-harness.mjs'),
                    os.environ['CODEX_WARMUP_BRIDGE_SOURCE'], str(root)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            lines = queue.Queue()
            reader = threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True)
            reader.start()

            def receive():
                return json.loads(lines.get(timeout=15))

            def control(action, message=''):
                proc.stdin.write(json.dumps({'action': action, 'message': message}) + '\n')
                proc.stdin.flush()
                return receive()

            try:
                ready = receive()
                self.assertTrue(ready['ready'])
                store = Store(root / 'plugin-state')
                configure(store, ready['cli'], shutil.which('node'))
                store.put('enabled', True)
                store.put('bound_account', 'test-account')
                now = int(time.time())
                store.put('resume_recovery', {'at': now - 10, 'account': 'test-account'})
                rpc = Mock()
                rpc.snapshot.return_value = snapshot(50, at=now)
                rpc.call.side_effect = lambda method, params: (
                    {'thread': {'id': ready['threadId'], 'name': 'Fixture only', 'model': 'fixture-original-model',
                                 'status': {'type': 'idle'}}} if method == 'thread/read' else
                    {'data': [{'id': ready['failedTurnId'], 'status': 'failed', 'completedAt': now - 100,
                               'error': {'codexErrorInfo': 'usageLimitExceeded'}}]})
                factory = Mock()
                factory.return_value.__enter__ = Mock(return_value=rpc)
                factory.return_value.__exit__ = Mock(return_value=False)
                message = '这是隔离夹具正文；保留此内容，不重发原任务'
                with patch('resume.time.time', return_value=now - 20):
                    save_rule(store, {'thread_id': ready['threadId'], 'message': message, 'confirm_send': True}, factory)
                bridge = RecoveryBridge(store)
                original_resume = bridge.resume

                def lose_reply(*args):
                    self.assertEqual(original_resume(*args)['status'], 'accepted')
                    raise BridgeError('BRIDGE_TIMEOUT')

                ordinary_sender = Mock()
                with patch.object(bridge, 'resume', side_effect=lose_reply) as resume_call:
                    dispatch(store, rpc, snapshot(50, at=now), now, ordinary_sender, lambda: True, lambda _: bridge)
                    dispatch(store, rpc, snapshot(50, at=now + 61), now + 61, ordinary_sender, lambda: True, lambda _: bridge)
                resume_call.assert_called_once()
                ordinary_sender.assert_not_called()
                self.assertEqual(rules(store)[0]['remaining_runs'], 0)
                self.assertEqual(rules(store)[0]['last_result']['state'], 'bridge_accepted')
                queued = control('pump', message)
                self.assertEqual(queued['calls'], 2)  # One mock original + one mock resumption.
                self.assertTrue(queued['hasCustomMessage'])
                self.assertFalse(queued['replayedOriginal'])
                self.assertIsNone(queued['quotaRemaining'])
                if ready['unlimited']:
                    self.assertEqual(ready['elapsedHours'], 48)
                    self.assertIsNone(queued['resumedRemaining'])
                else:
                    self.assertLessEqual(abs(queued['resumedRemaining'] - queued['originalRemaining']), 2000)
                # A fresh Store/adapter instance proves receipt identity survives restart.
                reopened = Store(root / 'plugin-state')
                reconcile_bridge(reopened, RecoveryBridge(reopened), now + 122)
                result = rules(reopened)[0]['last_result']
                self.assertEqual(result['state'], 'bridge_queued')
                self.assertTrue(result['queued'])
                self.assertFalse(result['execution_confirmed'])
                completed = control('complete')
                self.assertEqual(completed['completions'], 1)
                self.assertEqual(completed['calls'], 2)
                reconcile_bridge(reopened, RecoveryBridge(reopened), now + 183)
                result = rules(reopened)[0]['last_result']
                self.assertEqual(result['state'], 'bridge_completed')
                self.assertTrue(result['execution_confirmed'])
                self.assertFalse(result['bridge_pending'])
            finally:
                if proc.poll() is None:
                    try:
                        proc.stdin.write('{"action":"stop"}\n')
                        proc.stdin.flush()
                        proc.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        proc.terminate()
                        proc.wait(timeout=5)
                proc.stdin.close()
                reader.join(timeout=2)
                proc.stdout.close()


if __name__ == '__main__':
    unittest.main()
