"""Durable scheduler: local metadata polling, at-most-once sends, no LLM timer."""
from contextlib import closing
from datetime import datetime
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from runtime import Rpc, MODEL, EFFORT
from diagnostics import failure, health, record


def data_home():
    if os.environ.get("CODEX_WARMUP_HOME"):
        return Path(os.environ["CODEX_WARMUP_HOME"]).resolve()
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\CodexWarmup") as key:
                return Path(winreg.QueryValueEx(key, "DataDir")[0]).resolve()
        except OSError:
            pass
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexWarmup"


class Store:
    def __init__(self, directory=None):
        self.root = Path(directory or data_home()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.sqlite"
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actions (
                    account TEXT, cycle INTEGER, kind TEXT, at INTEGER, result TEXT,
                    PRIMARY KEY(account,cycle,kind));
                CREATE TABLE IF NOT EXISTS events (at INTEGER, name TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS leases (name TEXT PRIMARY KEY, owner TEXT, until INTEGER);
            ''')

    def db(self):
        return closing(sqlite3.connect(self.path, timeout=8, isolation_level=None))

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def event(self, name, detail=""):
        with self.db() as db:
            db.execute("INSERT INTO events VALUES (?,?,?)", (int(time.time()), name, detail))
            db.execute("DELETE FROM events WHERE rowid NOT IN (SELECT rowid FROM events ORDER BY rowid DESC LIMIT 500)")

    def claim(self, account, cycle, now, kind="warmup"):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM kv WHERE key='enabled'").fetchone()
            if kind == "warmup" and (not row or not json.loads(row[0])):
                db.execute("ROLLBACK")
                return False
            done = db.execute("SELECT MAX(cycle) FROM actions WHERE account=? AND kind=?", (account, kind)).fetchone()[0]
            if done is not None and done >= cycle:
                db.execute("ROLLBACK")
                return False
            db.execute("INSERT INTO actions VALUES (?,?,?,?,?)", (account, cycle, kind, now, "reserved_no_retry"))
            db.execute("COMMIT")
        return True

    def finish(self, account, cycle, result, kind="warmup"):
        with self.db() as db:
            db.execute("UPDATE actions SET result=? WHERE account=? AND cycle=? AND kind=?",
                       (json.dumps(result), account, cycle, kind))

    def lease(self, owner, now):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO leases VALUES ('worker',?,?)", (owner, now + 240))
            return db.execute("UPDATE leases SET owner=?,until=? WHERE name='worker' AND (owner=? OR until<?)",
                              (owner, now + 240, owner, now)).rowcount == 1

    def status(self):
        from resume import rules
        with self.db() as db:
            events = [{"at": a, "event": n, "detail": d} for a, n, d in db.execute(
                "SELECT at,name,detail FROM events ORDER BY rowid DESC LIMIT 8")]
            probe = db.execute("SELECT at,result FROM actions WHERE kind != 'warmup' ORDER BY at DESC LIMIT 1").fetchone()
        verification = None
        if probe and probe[1] != "reserved_no_retry":
            verification = {"at": probe[0], **json.loads(probe[1])}
        snap = self.get("snapshot")
        display = {}
        for key in ("primary", "secondary"):
            w = (snap or {}).get(key) or {}
            if type(w.get("usedPercent")) in (int, float) and type(w.get("resetsAt")) is int:
                display[key] = {"remaining_percent": 100 - w["usedPercent"],
                                "resets_local": datetime.fromtimestamp(w["resetsAt"]).astimezone().isoformat(timespec="seconds")}
        return {"enabled": self.get("enabled", False), "model": MODEL, "effort": EFFORT,
                "worker": self.get("worker"), "quota": snap, "readable_quota": display,
                "next_observed_reset": self.get("next_reset"), "last_result": self.get("last_result"),
                "transport_verification": verification, "last_check": self.get("last_check"),
                "login_autostart": self.get("autostart", False),
                "selected_chats": rules(self), "automatic_resume": "selected_recovery_rules",
                "collaboration_bridge": {"configured": self.get("recovery_bridge") is not None},
                "diagnostics": health(self), "resume_diagnostics": self.get("resume_diagnostics", {}),
                "low_quota_warning": self.get('low_quota_warning', {'enabled': False, 'threshold': 5}),
                "events": events, "data_dir": str(self.root)}


def valid(snapshot, now):
    if snapshot.get("auth") != "chatgpt" or snapshot.get("limit_id") != "codex":
        return False
    if not isinstance(snapshot.get("at"), (int, float)) or not 0 <= now - snapshot["at"] <= 120:
        return False
    primary = snapshot.get("primary") or {}
    if primary.get("windowDurationMins") != 300:
        return False
    for name in ("primary", "secondary"):
        w = snapshot.get(name) or {}
        used = w.get("usedPercent")
        reset = w.get("resetsAt")
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            return False
        if type(reset) is not int or reset <= 0:
            return False
    return True


def choose_cycle(snapshot, previous, next_reset, now):
    """Never infer quota recovery from the local clock alone."""
    if not valid(snapshot, now):
        return None, "quota_unknown_or_stale"
    if previous and not valid(previous, previous.get('at', now)):
        previous = None
    p, w = snapshot["primary"], snapshot["secondary"]
    if snapshot.get("blocked") or p["usedPercent"] >= 99.5 or w["usedPercent"] >= 99.5:
        return None, "waiting_for_included_quota"
    if p["usedPercent"] > 0:
        return None, "window_already_active"
    reset = p["resetsAt"]
    inferred = reset - 18000 if reset > now else reset
    if previous and previous.get("primary"):
        old = previous["primary"]
        # An official/extra reset can occur before the old predicted boundary.
        if old.get("usedPercent", 0) > 0 and old.get("resetsAt") != reset:
            return inferred if 0 < inferred <= now else now, "observed_extra_recovery"
        if (previous.get("blocked") or old.get("usedPercent", 0) >= 99.5
                or (previous.get("secondary") or {}).get("usedPercent", 0) >= 99.5):
            return now, "observed_blocking_quota_recovered"
    if next_reset and next_reset + 60 <= now:
        if now - next_reset <= 3600:
            return next_reset, "observed_scheduled_recovery"
        # After sleep/reboot, fresh empty provider data authorizes one catch-up.
        return inferred if 0 < inferred <= now else None, "fresh_empty_after_sleep"
    if not previous and 0 < inferred <= now:
        return inferred, "fresh_empty_on_enable"
    return None, "waiting_for_observed_reset"


def tick(store, rpc_factory=Rpc, now=None):
    wall_clock = now is None
    if not store.get("enabled", False):
        return "paused"
    with rpc_factory(store.root / "empty-workspace") as rpc:
        snap = rpc.snapshot()
        # Metadata may arrive seconds after this check began. Compare freshness
        # with the time AFTER it arrives, not a timestamp preceding the sample.
        now = int(time.time()) if now is None else now
        if snap["account"] != store.get("bound_account"):
            store.put("enabled", False)
            store.event("paused_account_changed")
            return "paused_account_changed"
        old = store.get("snapshot")
        next_reset = store.get("next_reset")
        from resume import observed_recovery, dispatch
        recovery = observed_recovery(snap, old, next_reset, now)
        if recovery:
            store.put('resume_recovery', recovery)
            store.event('resume_recovery_observed', 'tick')
        cycle, reason = choose_cycle(snap, old, next_reset, now)
        if cycle is not None:
            store.put("pending_cycle", {"cycle": cycle, "reason": reason})
        pending = store.get("pending_cycle")
        if pending and valid(snap, now) and not snap.get("blocked"):
            if snap["primary"]["usedPercent"] == 0 and snap["secondary"]["usedPercent"] < 99.5:
                cycle, reason = pending["cycle"], pending["reason"]
            elif snap["primary"]["usedPercent"] > 0:
                store.put("pending_cycle", None)
        store.put("snapshot", snap)
        if valid(snap, now) and snap["primary"]["resetsAt"] > now:
            store.put("next_reset", snap["primary"]["resetsAt"])
        store.put("last_check", {"at": now, "reason": reason})
        if cycle is None:
            dispatch(store, rpc, snap, now)
            return reason
        if not rpc.model_available():
            store.event("warmup_model_unavailable")
            dispatch(store, rpc, snap, now)
            return "warmup_model_unavailable"
        if not store.claim(snap["account"], cycle, now):
            store.put("pending_cycle", None)
            dispatch(store, rpc, snap, now)
            return "already_attempted_or_paused"
        store.put("pending_cycle", None)
        if not store.get("enabled", False):
            return "paused_before_send"
        store.event("sending_once", reason)
        try:
            result = rpc.warmup()
        except Exception as exc:
            result = {"request_completed": False, "error": type(exc).__name__, "ambiguous_no_retry": True}
            record(store, 'warmup_failed', failure(exc, 'warmup'), 'worker_last_error')
        store.finish(snap["account"], cycle, result)
        store.put("last_result", {"at": now, **result})
        store.event("request_finished", "completed" if result.get("request_completed") else "unconfirmed_no_retry")
        try:
            after = rpc.snapshot()
            if after["account"] == snap["account"] and valid(after, int(time.time())):
                store.put("snapshot", after)
                if after["primary"]["resetsAt"] > now:
                    store.put("next_reset", after["primary"]["resetsAt"])
        except Exception:
            store.event("post_read_unavailable")
        dispatch(store, rpc, store.get('snapshot') or snap, int(time.time()) if wall_clock else now)
        return "attempted_once"


def enable(store):
    with Rpc(store.root / "empty-workspace") as rpc:
        snap = rpc.snapshot()
        if not valid(snap, int(time.time())):
            raise RuntimeError("Cannot enable: verified ChatGPT five-hour/weekly quota unavailable")
        if not rpc.model_available():
            raise RuntimeError("Requested gpt-5.6-luna is not available; no fallback")
    # Capture a real recovery BEFORE replacing the previous quota observation.
    # This also covers a window already used by another chat while paused.
    from resume import observed_recovery
    if store.get('bound_account') == snap['account']:
        recovery = observed_recovery(snap, store.get('snapshot'), store.get('next_reset'), int(time.time()))
        if recovery:
            store.put('resume_recovery', recovery)
            store.event('resume_recovery_observed', 'enable')
    store.put("bound_account", snap["account"])
    # First nonempty window is observed, not warmed again unnecessarily.
    store.put("snapshot", snap if snap["primary"]["usedPercent"] > 0 else None)
    if snap["primary"]["resetsAt"] > snap["at"]:
        store.put("next_reset", snap["primary"]["resetsAt"])
    store.put("enabled", True)
    store.event("enabled")


def worker(store):
    owner = str(uuid.uuid4())
    started = False
    exit_reason = 'paused'
    try:
        while store.get("enabled", False):
            now = int(time.time())
            if not store.lease(owner, now):
                exit_reason = 'lease_owned_elsewhere'
                return
            if not started:
                started = True
                record(store, 'worker_started', {'owner': owner, 'parent_pid': os.getppid()}, 'worker_last_start')
            store.put("worker", {"pid": os.getpid(), "heartbeat": now, "state": "checking"})
            try:
                outcome = tick(store)
            except Exception as exc:
                outcome = "waiting_" + type(exc).__name__
                record(store, 'check_failed', failure(exc, 'check'), 'worker_last_error')
            store.put("worker", {"pid": os.getpid(), "heartbeat": int(time.time()), "state": outcome})
            for _ in range(60):
                if not store.get("enabled", False):
                    break
                time.sleep(1)
    except BaseException as exc:
        exit_reason = 'fatal_error'
        record(store, 'worker_failed', failure(exc, 'worker'), 'worker_last_error')
        raise
    finally:
        released = False
        try:
            with store.db() as db:
                released = db.execute("UPDATE leases SET until=0 WHERE name='worker' AND owner=?", (owner,)).rowcount
            if released:
                store.put("worker", {"pid": os.getpid(), "heartbeat": int(time.time()), "state": "stopped"})
        except Exception as exc:
            record(store, 'worker_cleanup_failed', failure(exc, 'cleanup'), 'worker_last_error')
        record(store, 'worker_exited', {'reason': exit_reason, 'owner': owner}, 'worker_last_exit' if started else None)
