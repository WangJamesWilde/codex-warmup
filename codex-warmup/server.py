"""Minimal MCP stdio controller and explicit local lifecycle commands."""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

# MCP transports UTF-8, not Windows' background-process CP936 default.
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from engine import Store, data_home, enable, tick, worker
from runtime import Rpc
from diagnostics import failure, process_alive, record

ROOT = Path(__file__).resolve().parent
VERSION = "0.6.0"
UI_URI = "ui://codex-warmup/manage-v0-6-0"


def icons():
    raw = (ROOT / "ui/icon.svg").read_bytes()
    return [{"src": "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii"),
             "mimeType": "image/svg+xml", "sizes": ["any"]}]


def resource(uri):
    if uri != UI_URI:
        raise ValueError("Unknown UI resource")
    return {"contents": [{"uri": UI_URI, "mimeType": "text/html;profile=mcp-app",
        "text": (ROOT / "ui/dist/app.html").read_text(encoding="utf-8"),
        "_meta": {"ui": {"csp": {"connectDomains": [], "resourceDomains": []}},
                  "openai/ui": {"availableDisplayModes": ["fullscreen"], "preferredDisplayMode": "fullscreen"}}}]}


def worker_script():
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\CodexWarmup") as key:
                p = Path(winreg.QueryValueEx(key, "SourceDir")[0]) / "server.py"
                if p.is_file():
                    return p
        except OSError:
            pass
    return ROOT / "server.py"


def launch(store):
    now = int(time.time())
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        def value(key, default=None):
            row = db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default
        if value('enabled', False) is not True:
            db.execute('COMMIT')
            return {'not_started': 'paused'}
        state = value('worker', {}) or {}
        started = value('worker_last_start', {}) or {}
        pid = state.get('pid')
        alive = process_alive(pid)
        # An overdue heartbeat is not permission to replace a living process.
        if alive is True or (pid is not None and alive is None):
            db.execute('COMMIT')
            return {'already_running': True} if alive else {'not_started': 'process_state_unknown'}
        previous = value('worker_last_launch', {}) or {}
        pending_pid = previous.get('worker_pid')
        pending_alive = process_alive(pending_pid) if pending_pid else None
        if pending_pid and pending_pid != pid and pending_alive is not False:
            db.execute('COMMIT')
            return {'not_started': 'launch_pending'}
        claim = db.execute("SELECT owner,until FROM leases WHERE name='worker_launch'").fetchone()
        dead_launch = (pending_pid == pid == started.get('pid') and pending_alive is False
                       and previous.get('launch_id') == (claim[0] if claim else None))
        if claim and claim[1] > now and not dead_launch:
            db.execute('COMMIT')
            return {'not_started': 'launch_pending'}
        lease = db.execute("SELECT owner,until FROM leases WHERE name='worker'").fetchone()
        if lease and lease[1] > now:
            # A hard-killed worker cannot release its four-minute lease. Reclaim
            # only the exact lease whose recorded owner has been confirmed dead.
            if alive is False and started.get('pid') == pid and started.get('owner') == lease[0]:
                db.execute("UPDATE leases SET until=0 WHERE name='worker' AND owner=?", (lease[0],))
            else:
                db.execute('COMMIT')
                return {'not_started': 'worker_lease_unconfirmed'}
        # Several chats may initialize MCP simultaneously. Reserve the launch
        # before spawning, including the gap before the first worker heartbeat.
        launch_id = str(uuid.uuid4())
        db.execute("INSERT OR REPLACE INTO leases VALUES ('worker_launch',?,?)", (launch_id, now + 15))
        db.execute('COMMIT')
    python = Path(sys.executable)
    if os.name == "nt" and python.with_name("pythonw.exe").is_file():
        python = python.with_name("pythonw.exe")
    try:
        proc = subprocess.Popen([str(python), "-B", str(worker_script()), "--data-dir", str(store.root), "worker"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd=store.root,
                                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0)
    except OSError as exc:
        record(store, 'worker_launch_failed', failure(exc, 'launch'), 'worker_last_error')
        raise
    record(store, 'worker_launched', {'worker_pid': proc.pid, 'version': VERSION, 'launch_id': launch_id}, 'worker_last_launch')
    return {"started_pid": proc.pid}


def recover_worker(store):
    """Restore existing consent on MCP load; never re-enable or rebind an account."""
    try:
        if store.get('enabled', False) is not True:
            return {'not_started': 'paused'}
        result = launch(store)
        if result.get('started_pid'):
            record(store, 'worker_auto_recovered', {'worker_pid': result['started_pid'], 'trigger': 'plugin_loaded'})
        return result
    except Exception as exc:
        # A background launch failure must not break MCP discovery or the UI
        # through which the user can inspect diagnostics and pause the plugin.
        record(store, 'worker_recovery_failed', failure(exc, 'plugin_load'), 'worker_last_error')
        return {'not_started': 'launch_failed'}


def autostart(store, enabled):
    if os.name != "nt":
        raise RuntimeError("Login autostart is implemented only for Windows")
    import winreg
    python = Path(sys.executable)
    if python.with_name("pythonw.exe").is_file():
        python = python.with_name("pythonw.exe")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\CodexWarmup") as key:
        winreg.SetValueEx(key, "DataDir", 0, winreg.REG_SZ, str(store.root))
        winreg.SetValueEx(key, "SourceDir", 0, winreg.REG_SZ, str(ROOT))
    value = subprocess.list2cmdline([str(python), "-B", str(ROOT / "server.py"), "--data-dir", str(store.root), "worker"])
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        try:
            existing = winreg.QueryValueEx(key, "CodexWarmup")[0]
        except FileNotFoundError:
            existing = None
        if existing and existing != value:
            raise RuntimeError("Unrecognized existing CodexWarmup login entry; left unchanged")
        if enabled:
            winreg.SetValueEx(key, "CodexWarmup", 0, winreg.REG_SZ, value)
        elif existing:
            winreg.DeleteValue(key, "CodexWarmup")
    store.put("autostart", enabled)
    return {"login_autostart": enabled}


TOOLS = [
    {"name": "warmup_manage", "title": "预热管理", "description": "从原生导航打开本地预热管理页面，不发送模型请求。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False, "destructiveHint": False},
     "icons": icons(),
     "_meta": {"ui": {"resourceUri": UI_URI, "visibility": ["app"]},
               "openai/ui": {"entrypoints": [{"type": "global"}]}, "openai/iconStyle": "monochrome"}},
    {"name": "warmup_chats", "title": "选择续聊对话", "description": "只读搜索本机已有对话，不发送消息。",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 120}, "cursor": {"type": "string"}}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}, "_meta": {"ui": {"visibility": ["app", "model"]}}},
    {"name": "warmup_rule_save", "title": "保存续聊规则", "description": "经用户明确同意保存选定对话及消息。trigger=after_recovery是额度恢复后发送的预输入，允许正常收尾；quota_stop仅恢复额度中断。省略保留原规则条件，新规则省略仍为quota_stop。默认单次，保存不发消息。",
     "inputSchema": {"type": "object", "properties": {"thread_id": {"type": "string"}, "message": {"type": "string", "maxLength": 4000},
                     "repeat": {"type": "boolean"}, "enabled": {"type": "boolean"}, "confirm_send": {"type": "boolean"},
                     "trigger": {"type": "string", "enum": ["quota_stop", "after_recovery"]},
                     "run_limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 1000}, "expected_revision": {"type": "integer", "minimum": 0}},
                     "required": ["thread_id", "message", "confirm_send"], "additionalProperties": False}},
    {"name": "warmup_rule_set_enabled", "title": "快捷加入或取消续聊", "description": "按用户点击设定指定对话的自动续聊开关。首次加入默认继续、单次，再次加入保留原消息及重复设置；取消只停用，不删除配置，不立即发消息。",
     "inputSchema": {"type": "object", "properties": {"thread_id": {"type": "string"}, "enabled": {"type": "boolean"},
                     "expected_revision": {"type": "integer", "minimum": 0}, "confirm_send": {"type": "boolean"}},
                     "required": ["thread_id", "enabled", "expected_revision"], "additionalProperties": False}},
    {"name": "warmup_rule_set_mode", "title": "设置续聊次数", "description": "由用户选择单次、每次恢复后重复或指定执行次数；不使用账号重置券，不立即发送，不改变规则启用状态。",
     "inputSchema": {"type": "object", "properties": {"rule_id": {"type": "string"}, "expected_revision": {"type": "integer", "minimum": 1},
                     "run_limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 1000}, "confirm_send": {"type": "boolean"}},
                     "required": ["rule_id", "expected_revision", "run_limit", "confirm_send"], "additionalProperties": False}},
    {"name": "warmup_rule_remove", "title": "移除续聊规则", "description": "移除用户指定的本地续聊规则，不删除对话或发送历史。",
     "inputSchema": {"type": "object", "properties": {"rule_id": {"type": "string"}}, "required": ["rule_id"], "additionalProperties": False}},
    {"name": "warmup_status", "description": "查看常驻预热状态、真实额度快照和最近结果；不会发模型请求。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "warmup_warning_configure", "title": "设置低额度提醒", "description": "按用户选择开启或关闭工具执行后当前回合的低额度提示，阈值3%或5%，每周期每对话一次。不发聊天、不保存续聊规则，仍需用户审阅并信任插件Hook。",
     "inputSchema": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "threshold": {"type": "integer", "enum": [3, 5]}},
                     "required": ["enabled", "threshold"], "additionalProperties": False}},
    {"name": "warmup_enable", "description": "经用户明确授权启用本地后台；预热仅gpt-5.6-luna low，额度恢复后按用户保存的触发条件续接选定对话，不修改模型和权限。",
     "inputSchema": {"type": "object", "properties": {"confirm_quota_spend": {"type": "boolean"}},
                     "required": ["confirm_quota_spend"], "additionalProperties": False}},
    {"name": "warmup_pause", "description": "暂停常驻预热，后台当前操作结束后退出。不会中断桌面端其他对话。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "warmup_refresh", "description": "只读刷新当前额度和登录绑定状态，不发送模型请求、不使用重置券。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
]


def tool(store, name, args):
    if name == 'warmup_warning_configure':
        from quota_warning import configure
        return configure(store, args)
    if name in ("warmup_status", "warmup_manage"):
        return store.status()
    if name == 'warmup_chats':
        query = args.get('query', '')
        if not isinstance(query, str) or len(query) > 120:
            raise ValueError('Invalid search')
        params = {'limit': 50, 'sortKey': 'updated_at', 'archived': False, 'useStateDbOnly': True,
                  'searchTerm': query, 'cursor': args.get('cursor')}
        with Rpc(store.root / 'empty-workspace') as rpc:
            page = rpc.call('thread/list', params)
        return {'chats': [{'id': row['id'], 'title': row.get('name') or '未命名对话', 'model': row.get('model'),
                           'effort': row.get('reasoningEffort'), 'cwd': row.get('cwd')}
                          for row in page.get('data', []) if not row.get('parentThreadId') and not row.get('ephemeral')],
                'next_cursor': page.get('nextCursor'), 'message_sent': False}
    if name == 'warmup_rule_save':
        from resume import save_rule
        return save_rule(store, args, Rpc)
    if name == 'warmup_rule_set_enabled':
        from resume import set_rule_enabled
        return set_rule_enabled(store, args, Rpc)
    if name == 'warmup_rule_set_mode':
        from resume import set_rule_mode
        return set_rule_mode(store, args)
    if name == 'warmup_rule_remove':
        from resume import remove_rule
        return remove_rule(store, args.get('rule_id'))
    if name == "warmup_enable":
        if args != {"confirm_quota_spend": True}:
            raise ValueError("Explicit authorization required")
        enable(store)
        return {**launch(store), "enabled": True}
    if name == "warmup_pause":
        store.put("enabled", False)
        store.event("paused_by_user")
        return {"enabled": False, "stops_after_current_operation": True}
    if name == "warmup_refresh":
        with Rpc(store.root / "empty-workspace") as rpc:
            snapshot = rpc.snapshot()
        # A refresh must not overwrite the scheduler's previous observation.
        return {"snapshot": snapshot, "model_request_sent": False}
    raise ValueError("Unknown tool")


def mcp(store):
    initialized = recovery_checked = False
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                continue
            if request.get('method') == 'notifications/initialized' and 'id' not in request:
                if initialized and not recovery_checked:
                    recovery_checked = True
                    recover_worker(store)
                continue
            if 'id' not in request:
                continue
            rid, method = request["id"], request.get("method")
            if method == "initialize":
                version = (request.get("params") or {}).get("protocolVersion", "2024-11-05")
                if version not in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
                    version = "2025-11-25"
                result = {"protocolVersion": version, "capabilities": {"tools": {}, "resources": {}},
                          "serverInfo": {"name": "codex-warmup", "title": "预热管理", "version": VERSION, "icons": icons()}}
                initialized = True
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "resources/list":
                result = {"resources": [{"uri": UI_URI, "name": "warmup-manager", "title": "预热管理",
                                         "mimeType": "text/html;profile=mcp-app"}]}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "resources/read":
                try:
                    result = resource((request.get("params") or {}).get("uri"))
                except (ValueError, OSError):
                    print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "UI resource unavailable"}}), flush=True)
                    continue
            elif method == "tools/call":
                params = request.get("params") or {}
                try:
                    value = tool(store, params.get("name"), params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
                              "structuredContent": value}
                except Exception as exc:
                    result = {"isError": True, "content": [{"type": "text", "text": "Operation failed: " + type(exc).__name__}]}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False), flush=True)
        except (ValueError, KeyError, TypeError):
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid request"}}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    parser.add_argument("--probe-id", default="supervised_probe", help="Separate probe ID only after new explicit user approval")
    parser.add_argument("--probe-timeout", type=int, default=90)
    parser.add_argument("--bridge-cli", help="Absolute path to the trusted local Aion recovery CLI")
    parser.add_argument("--node-path", help="Absolute Node executable path; otherwise discover on PATH")
    parser.add_argument("command", nargs="?", default="mcp",
                        choices=["mcp", "status", "enable", "pause", "worker", "autostart-on", "autostart-off", "check", "verify-send", "bridge-config"])
    args = parser.parse_args()
    store = Store(args.data_dir)
    if args.command == "mcp":
        mcp(store)
    elif args.command == "worker":
        worker(store)
    elif args.command == "status":
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
    elif args.command == "pause":
        print(json.dumps(tool(store, "warmup_pause", {})))
    elif args.command == "enable":
        enable(store)
        print(json.dumps(launch(store)))
    elif args.command.startswith("autostart-"):
        print(json.dumps(autostart(store, args.command == "autostart-on")))
    elif args.command == "check":
        print(json.dumps({"reason": tick(store)}))
    elif args.command == "bridge-config":
        if not args.bridge_cli:
            parser.error("bridge-config requires --bridge-cli")
        from collaboration import configure
        print(json.dumps(configure(store, args.bridge_cli, args.node_path)))
    elif args.command == "verify-send":
        # This explicit CLI command is for ONE supervised transport test only.
        with Rpc(store.root / "empty-workspace") as rpc:
            snapshot = rpc.snapshot()
            from engine import valid
            if not valid(snapshot, int(time.time())) or snapshot.get("blocked"):
                raise RuntimeError("Included quota unavailable")
            if any(snapshot[k]["usedPercent"] >= 99.5 for k in ("primary", "secondary")):
                raise RuntimeError("Included quota exhausted")
            if not rpc.model_available():
                raise RuntimeError("Requested model unavailable")
            if not store.claim(snapshot["account"], 1, int(time.time()), args.probe_id):
                raise RuntimeError("Probe already attempted; no retry")
            try:
                result = rpc.warmup(timeout=args.probe_timeout)
            except Exception as exc:
                result = {"request_completed": False, "error": type(exc).__name__, "ambiguous_no_retry": True}
            store.finish(snapshot["account"], 1, result, args.probe_id)
            print(json.dumps(result))


if __name__ == "__main__":
    main()
