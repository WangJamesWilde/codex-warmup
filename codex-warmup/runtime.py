"""Local Codex transport. No direct HTTP/auth.json, API keys or reset-credit API."""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
from urllib.request import getproxies

MODEL = "gpt-5.6-luna"
EFFORT = "low"


class MetadataError(RuntimeError):
    def __init__(self, method, code=None):
        super().__init__('Codex metadata request failed: ' + method)
        self.rpc_method = method
        self.rpc_code = code


def desktop_running():
    if os.name != 'nt':
        return False
    # Inspect process metadata, not windows, credentials, or UI controls. Do not
    # launch the desktop app automatically merely to deliver queued messages.
    script = "@(Get-Process -Name ChatGPT,Codex -ErrorAction SilentlyContinue | Where-Object { $_.Path -match '\\\\app\\\\(ChatGPT|Codex)\\.exe$' }).Count -gt 0"
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, timeout=10, encoding='utf-8', errors='replace',
                                creationflags=subprocess.CREATE_NO_WINDOW)
        return result.returncode == 0 and result.stdout.strip().lower() == 'true'
    except (OSError, subprocess.TimeoutExpired):
        return False


def queue_message(thread_id, message):
    # The queue command uses the existing chat's model. No model, permissions,
    # reset-credit, account, or tool-approval overrides are supplied.
    try:
        result = subprocess.run([str(executable()), 'queue', '--thread', thread_id, '--message', message],
                                env=environment(), stdin=subprocess.DEVNULL, capture_output=True,
                                encoding='utf-8', errors='replace', timeout=40,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        accepted = result.returncode == 0 and 'Queued message ' in result.stdout
        return {'queued': accepted, 'state': 'queued' if accepted else 'unconfirmed_no_retry',
                'execution_confirmed': False}
    except subprocess.TimeoutExpired:
        return {'queued': False, 'state': 'unconfirmed_no_retry', 'execution_confirmed': False}


def warmup_command(exe, cwd):
    args = [str(exe), "exec", "--json", "--ignore-user-config", "--ignore-rules", "--ephemeral",
            "--skip-git-repo-check", "--sandbox", "read-only", "--cd", str(cwd),
            "--model", MODEL, "-c", 'model_reasoning_effort="low"',
            "-c", 'approval_policy="never"', "-c", "project_doc_max_bytes=0"]
    args += ["Hi. Reply only with hi. Do not use tools or read files."]
    return args


def classify_turn(output, code):
    completed = replied = failed = tools = False
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in ("error", "turn.failed"):
            failed = True
        if kind == "turn.completed":
            completed = True
        row = item.get("item") or {}
        if kind == "item.completed" and isinstance(row, dict):
            if row.get("type") == "agent_message" and str(row.get("text", "")).strip():
                replied = True
            elif row.get("type") in ("command_execution", "mcp_tool_call", "web_search", "file_change"):
                tools = True
    return {"request_completed": code == 0 and completed and replied and not failed,
            "tool_observed": tools, "window_start_verified": False}


def executable():
    override = os.environ.get("CODEX_WARMUP_CODEX")
    if override:
        p = Path(override).resolve(strict=True)
        if p.name.lower() != "codex.exe":
            raise ValueError("Expected native codex.exe")
        return p
    p = shutil.which("codex.exe")
    if p:
        return Path(p).resolve()
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
    candidates = list(base.glob("*/codex.exe"))
    if not candidates:
        raise FileNotFoundError("Codex CLI unavailable; waiting for installation/path repair")
    return max(candidates, key=lambda x: x.stat().st_mtime).resolve()


def environment():
    env = dict(os.environ)
    # The worker is a standalone program, not another turn in the launching
    # Codex task. Do not inherit its host pipe/session/originator identity.
    for key in list(env):
        if key.startswith("CODEX_") and key not in ("CODEX_HOME", "CODEX_WARMUP_HOME", "CODEX_WARMUP_CODEX"):
            env.pop(key, None)
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL",
                "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"):
        env.pop(key, None)
    # The HTTP metadata client can use WinINet while the WebSocket client may
    # not. Pass the existing OS proxy to this child only, without changing it.
    proxies = getproxies()
    for scheme in ("http", "https", "all"):
        key = scheme.upper() + "_PROXY"
        if proxies.get(scheme) and not any(k.upper() == key for k in env):
            env[key] = proxies[scheme]
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class Rpc:
    def __init__(self, cwd):
        self.cwd = Path(cwd)
        self.proc = None
        self.inbox = queue.Queue()
        self.events = []
        self.seq = 0

    def __enter__(self):
        self.cwd.mkdir(parents=True, exist_ok=True)
        args = [str(executable()), "app-server"]
        # These switches apply ONLY to this isolated helper, never desktop chats.
        for flag in ("apps", "plugins", "hooks", "memories", "multi_agent", "shell_tool",
                     "unified_exec", "browser_use", "computer_use", "image_generation", "skill_search"):
            args += ["--disable", flag]
        args += ["-c", "project_doc_max_bytes=0"]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=self.cwd, env=environment(),
                                     encoding="utf-8", errors="replace",
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        def reader():
            try:
                for line in self.proc.stdout:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict):
                        self.inbox.put(item)
            finally:
                self.inbox.put(None)
        threading.Thread(target=reader, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "codex_warmup", "version": "0.1.0"}})
            self.send({"method": "initialized", "params": {}})
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def send(self, item):
        self.proc.stdin.write(json.dumps(item) + "\n")
        self.proc.stdin.flush()

    def receive(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex helper timeout; ambiguous sends are not retried")
        try:
            item = self.inbox.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("Codex helper timeout") from None
        if item is None:
            raise RuntimeError("Codex helper disconnected")
        if "id" in item and "method" in item:
            # Never approve a tool, provide credentials, or answer questions silently.
            self.send({"id": item["id"], "error": {"code": -32601, "message": "Not authorized by warm-up helper"}})
            return {}
        return item

    def call(self, method, params=None, timeout=40):
        self.seq += 1
        rid = self.seq
        self.send({"id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            try:
                item = self.receive(deadline)
            except (TimeoutError, RuntimeError, OSError) as exc:
                # Keep the stage, not the upstream body or any credentials.
                exc.rpc_method = method
                raise
            if item.get("id") == rid:
                if "error" in item:
                    raise MetadataError(method, (item['error'] or {}).get('code'))
                return item.get("result") or {}
            if "method" in item:
                self.events.append(item)

    def snapshot(self):
        account = self.call("account/read", {"refreshToken": False}).get("account") or {}
        raw = self.call("account/rateLimits/read")
        bucket = (raw.get("rateLimitsByLimitId") or {}).get("codex") or raw.get("rateLimits") or {}
        # A digest detects sign-in changes without retaining email/account identity.
        identity = "|".join(str(account.get(k) or "") for k in ("type", "email", "id", "accountId"))
        if not account.get("email") and not account.get("id") and not account.get("accountId"):
            raise RuntimeError("Account identity unavailable; cannot bind automation safely")
        return {"at": int(time.time()), "account": hashlib.sha256(identity.encode()).hexdigest(),
                "auth": account.get("type"), "plan": account.get("planType"),
                "primary": bucket.get("primary"), "secondary": bucket.get("secondary"),
                "blocked": bool(bucket.get("spendControlReached") or bucket.get("rateLimitReachedType")),
                "limit_id": bucket.get("limitId")}

    def model_available(self):
        rows = self.call("model/list", {"limit": 100}).get("data", [])
        return any(row.get("model") == MODEL and not row.get("hidden") for row in rows)

    def warmup(self, timeout=90):
        # Use Deck's already tested exec route; metadata RPC is not a model runner.
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(warmup_command(executable(), self.cwd), cwd=self.cwd, env=environment(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                encoding="utf-8", errors="replace", creationflags=flags)
        try:
            output, errors = proc.communicate(timeout=timeout)
            return classify_turn(output, proc.returncode)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=8, creationflags=flags, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                if proc.poll() is None:
                    proc.kill()
                try:
                    output, errors = proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    output, errors = "", ""
            combined = (output + errors).lower()
            signals = [s for s in ("unauthorized", "401", "429", "quota", "rate limit", "reconnect",
                                   "stream disconnected", "websocket", "not supported", "not found", "timeout") if s in combined]
            kinds = []
            messages = []
            for line in output.splitlines():
                try:
                    event = json.loads(line)
                    kinds.append(event.get("type"))
                    if event.get("type") == "error":
                        message = str(event.get("message", ""))
                        message = re.sub(r"(?i)bearer\s+\S+|sk-[\w-]{8,}|eyJ[\w-]+\.[\w-]+\.[\w-]+", "[redacted]", message)
                        messages.append(message[:350])
                except (ValueError, AttributeError):
                    pass
            hints = []
            for line in errors.splitlines():
                if any(s in line.lower() for s in ("not supported", "error", "failed", "unauthorized")):
                    clean = re.sub(r"(?i)bearer\s+\S+|sk-[\w-]{8,}|eyJ[\w-]+\.[\w-]+\.[\w-]+", "[redacted]", line)
                    hints.append(clean[:450])
            return {"request_completed": False, "ambiguous_no_retry": True, "error": "TimeoutError",
                    "diagnostic_signals": signals, "event_types": kinds[-12:], "messages": messages[:3],
                    "diagnostic_hints": hints[:3]}

    def __exit__(self, *_):
        if not self.proc:
            return
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        finally:
            self.proc.stdout.close()
