import concurrent.futures
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from engine import Store, valid, choose_cycle, tick
from runtime import warmup_command, classify_turn, environment
import server

NOW = 20000


def snapshot(used=0, weekly=10, reset=37000, at=NOW, account="test-account"):
    return {"at": at, "auth": "chatgpt", "plan": "plus", "limit_id": "codex", "account": account,
            "blocked": False, "primary": {"usedPercent": used, "windowDurationMins": 300, "resetsAt": reset},
            "secondary": {"usedPercent": weekly, "windowDurationMins": 10080, "resetsAt": 600000}}


class Policy(unittest.TestCase):
    def test_child_uses_os_proxy_without_mutating_parent(self):
        with patch.dict('os.environ', {}, clear=True), patch('runtime.getproxies', return_value={
                "http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}):
            env = environment()
            import os
            self.assertNotIn("HTTPS_PROXY", os.environ)
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7890")

    def test_explicit_child_proxy_is_not_overwritten(self):
        with patch.dict('os.environ', {"HTTPS_PROXY": "http://127.0.0.1:8000"}, clear=True), patch(
                'runtime.getproxies', return_value={"https": "http://127.0.0.1:7890"}):
            self.assertEqual(environment()["HTTPS_PROXY"], "http://127.0.0.1:8000")

    def test_worker_detaches_from_launching_codex_task(self):
        with patch.dict('os.environ', {"CODEX_THREAD_ID": "fictional", "CODEX_APP_TOOLS_PIPE_PATH": "fictional",
                                      "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "fictional", "CODEX_HOME": "login-home"}):
            env = environment()
        self.assertNotIn("CODEX_THREAD_ID", env)
        self.assertNotIn("CODEX_APP_TOOLS_PIPE_PATH", env)
        self.assertNotIn("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", env)
        self.assertEqual(env["CODEX_HOME"], "login-home")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
    def test_transport_defaults_are_scoped_and_not_resume(self):
        args = warmup_command(Path("codex.exe"), Path("empty"))
        self.assertEqual(args[1], "exec")
        self.assertNotIn("resume", args)
        self.assertIn("gpt-5.6-luna", args)
        self.assertIn('model_reasoning_effort="low"', args)
        self.assertIn("--ignore-user-config", args)
        self.assertNotIn("danger-full-access", args)

    def test_reply_does_not_prove_quota_window(self):
        output = '\n'.join([json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"hi"}}),
                            json.dumps({"type":"turn.completed"})])
        self.assertTrue(classify_turn(output, 0)["request_completed"])
        self.assertFalse(classify_turn(output, 0)["window_start_verified"])
        self.assertFalse(classify_turn(output+'\n'+json.dumps({"type":"error"}), 0)["request_completed"])
    def test_active_window_no_request(self):
        self.assertEqual(choose_cycle(snapshot(65), None, None, NOW)[1], "window_already_active")

    def test_weekly_blocks_even_after_five_hours(self):
        self.assertIsNone(choose_cycle(snapshot(0, 100), snapshot(100), 19000, NOW)[0])

    def test_scheduled_reset_and_grace(self):
        self.assertEqual(choose_cycle(snapshot(), snapshot(100, reset=19000), 19000, NOW)[0], 19000)
        self.assertIsNone(choose_cycle(snapshot(), snapshot(0), 19960, NOW)[0])

    def test_extra_recovery_before_old_reset(self):
        cycle, reason = choose_cycle(snapshot(reset=38000), snapshot(50, reset=40000), 40000, NOW)
        self.assertIsNotNone(cycle)
        self.assertEqual(reason, "observed_extra_recovery")

    def test_weekly_recovery_is_observed(self):
        cycle, reason = choose_cycle(snapshot(), snapshot(0, 100), 37000, NOW)
        self.assertEqual((cycle, reason), (NOW, "observed_blocking_quota_recovered"))

    def test_initial_empty_is_eligible_but_not_future_start(self):
        self.assertEqual(choose_cycle(snapshot(reset=37000), None, None, NOW)[0], 19000)
        self.assertIsNone(choose_cycle(snapshot(reset=50000), None, None, NOW)[0])

    def test_clock_passed_does_not_override_unknown_data(self):
        s = snapshot()
        s["primary"]["usedPercent"] = None
        self.assertIsNone(choose_cycle(s, snapshot(100), 19000, NOW)[0])

    def test_bad_auth_wrong_window_stale_and_nonfinite_fail_closed(self):
        for field in ("auth", "at", "limit_id", "nan", "duration"):
            s = snapshot()
            if field == "auth": s["auth"] = "apiKey"
            elif field == "at": s["at"] = NOW - 121
            elif field == "limit_id": s["limit_id"] = "other"
            elif field == "duration": s["primary"]["windowDurationMins"] = 60
            else: s["primary"]["usedPercent"] = float("nan")
            with self.subTest(field=field): self.assertFalse(valid(s, NOW))


class Durable(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).parent.parent / ".state"
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.store = Store(self.temp.name)
        self.store.put("enabled", True)
        self.store.put("bound_account", "test-account")

    def tearDown(self): self.temp.cleanup()

    def rpc(self, snap, failure=False, available=True):
        rpc = Mock()
        rpc.snapshot.return_value = snap
        rpc.model_available.return_value = available
        if failure: rpc.warmup.side_effect = TimeoutError()
        else: rpc.warmup.return_value = {"request_completed": True, "window_start_verified": False}
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=rpc)
        factory.return_value.__exit__ = Mock(return_value=False)
        return rpc, factory

    def test_eight_process_claimers_only_one_send(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.claim("test-account", 19000, NOW), range(8)))
        self.assertEqual(sum(results), 1)
        self.assertFalse(self.store.claim("test-account", 18000, NOW))

    def test_same_cycle_not_resent_after_restart(self):
        rpc, factory = self.rpc(snapshot())
        tick(self.store, factory, NOW)
        reopened = Store(self.temp.name)
        tick(reopened, factory, NOW)
        rpc.warmup.assert_called_once()

    def test_snapshot_freshness_clock_is_after_metadata_arrives(self):
        rpc, factory = self.rpc(snapshot(50, at=NOW + 3))
        with patch('engine.time.time', return_value=NOW) as clock:
            def fetch():
                clock.return_value = NOW + 3
                return snapshot(50, at=NOW + 3)
            rpc.snapshot.side_effect = fetch
            self.assertEqual(tick(self.store, factory), "window_already_active")
        rpc.warmup.assert_not_called()

    def test_ambiguous_timeout_not_retried(self):
        rpc, factory = self.rpc(snapshot(), failure=True)
        tick(self.store, factory, NOW)
        tick(self.store, factory, NOW)
        rpc.warmup.assert_called_once()
        self.assertTrue(self.store.get("last_result")["ambiguous_no_retry"])

    def test_missing_model_pending_cycle_not_lost(self):
        self.store.put("next_reset", 19000)
        self.store.put("snapshot", snapshot(100, reset=19000))
        rpc, factory = self.rpc(snapshot(), available=False)
        self.assertEqual(tick(self.store, factory, NOW), "warmup_model_unavailable")
        rpc.model_available.return_value = True
        tick(self.store, factory, NOW)
        rpc.warmup.assert_called_once()

    def test_account_change_pauses_without_sending(self):
        rpc, factory = self.rpc(snapshot(account="different"))
        self.assertEqual(tick(self.store, factory, NOW), "paused_account_changed")
        self.assertFalse(self.store.get("enabled"))
        rpc.warmup.assert_not_called()

    def test_paused_does_not_even_start_helper(self):
        self.store.put("enabled", False)
        _, factory = self.rpc(snapshot())
        self.assertEqual(tick(self.store, factory, NOW), "paused")
        factory.assert_not_called()

    def test_readonly_refresh_does_not_lose_scheduling_observation(self):
        self.store.put("snapshot", snapshot(100))
        rpc, factory = self.rpc(snapshot())
        with patch.object(server, "Rpc", factory):
            server.tool(self.store, "warmup_refresh", {})
        self.assertEqual(self.store.get("snapshot")["primary"]["usedPercent"], 100)

    def test_single_worker_lease(self):
        self.assertTrue(self.store.lease("one", NOW))
        self.assertFalse(self.store.lease("two", NOW + 60))
        self.assertTrue(self.store.lease("two", NOW + 241))

    def test_status_separates_transport_probe_from_automatic_send(self):
        self.store.claim("test-account", 1, NOW, "approved-probe")
        result = {"request_completed": True, "tool_observed": False, "window_start_verified": False}
        self.store.finish("test-account", 1, result, "approved-probe")
        status = self.store.status()
        self.assertTrue(status["transport_verification"]["request_completed"])
        self.assertFalse(status["transport_verification"]["window_start_verified"])
        self.assertIsNone(status["last_result"])

    def test_enable_tool_needs_explicit_confirmation(self):
        with self.assertRaises(ValueError):
            server.tool(self.store, "warmup_enable", {})

    def test_plugin_manifests_and_mcp_stdio_roundtrip(self):
        # Discovery must not opt this subprocess fixture into real background work.
        self.store.put('enabled', False)
        root = Path(__file__).parent
        manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
        legacy = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], legacy["name"])
        self.assertEqual(manifest["version"], legacy["version"])
        self.assertLessEqual(len(manifest["extensions"]["com.openai"]["interface"]["shortDescription"]), 30)
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "warmup_status", "arguments": {}}},
        ]
        import os
        child = dict(os.environ, PYTHONIOENCODING="gbk")
        proc = subprocess.run([sys.executable, "-B", str(root / "server.py"), "--data-dir", self.temp.name], env=child,
                              input="\n".join(json.dumps(x) for x in lines) + "\n", capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        messages = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([x["id"] for x in messages], [1, 2, 3])
        self.assertEqual(len(messages[1]["result"]["tools"]), 11)
        self.assertFalse(messages[2]["result"].get("isError", False))

    def test_native_global_entrypoint_is_not_thread_browser_or_tray(self):
        entry = next(tool for tool in server.TOOLS if tool['name'] == 'warmup_manage')
        self.assertEqual(entry['_meta']['openai/ui']['entrypoints'], [{'type': 'global'}])
        self.assertEqual(entry['_meta']['ui']['visibility'], ['app'])
        view = server.tool(self.store, 'warmup_manage', {})
        self.assertIsNone(view['last_result'])
        self.assertEqual(view['selected_chats'], [])

    def test_ui_resource_is_offline_and_path_allowlisted(self):
        ui = server.resource(server.UI_URI)['contents'][0]
        self.assertIn('text/html', ui['mimeType'])
        self.assertIn('预热管理', ui['text'])
        self.assertIn('MCP App', ui['text'])
        self.assertEqual(ui['_meta']['openai/ui']['preferredDisplayMode'], 'fullscreen')
        self.assertEqual(ui['_meta']['ui']['csp']['connectDomains'], [])
        with self.assertRaises(ValueError):
            server.resource('file:///auth.json')


if __name__ == "__main__": unittest.main()
