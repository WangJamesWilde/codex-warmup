"""Optional, non-blocking tool-boundary warning; no RPC, credentials or tool text."""
import json
from pathlib import Path
import sqlite3
import time
import uuid

from engine import data_home, valid


def configure(store, data):
    enabled, threshold = data.get('enabled'), data.get('threshold', 5)
    if type(enabled) is not bool or type(threshold) is not int or threshold not in (3, 5):
        raise ValueError('Choose enabled and a threshold of 3 or 5 percent')
    value = {'enabled': enabled, 'threshold': threshold}
    store.put('low_quota_warning', value)
    return {'low_quota_warning': value, 'requires_hook_trust': True, 'message_sent': False}


def warning(event, directory=None, now=None, caller_thread_id=None):
    if not isinstance(event, dict) or event.get('hook_event_name') != 'PostToolUse':
        return {}
    try:
        thread_id = str(uuid.UUID(event.get('session_id', '')))
    except (ValueError, TypeError, AttributeError):
        return {}
    if any(event.get(key) for key in ('agent_id', 'agent_type', 'subagent_id')):
        return {}
    if caller_thread_id and caller_thread_id != thread_id:
        return {}
    if not isinstance(event.get('turn_id'), str) or not event['turn_id']:
        return {}
    if not isinstance(event.get('model'), str) or not event['model'].startswith('gpt-'):
        return {}
    now = int(time.time()) if now is None else now
    path = (Path(directory) if directory else data_home()) / 'state.sqlite'
    # Never create a new runtime just because an unrelated chat used a tool.
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=.15, isolation_level=None)
    try:
        db.execute('BEGIN IMMEDIATE')
        values = {key: json.loads(value) for key, value in db.execute(
            "SELECT key,value FROM kv WHERE key IN ('enabled','low_quota_warning','snapshot','bound_account')")}
        config = values.get('low_quota_warning') or {}
        if not values.get('enabled') or config.get('enabled') is not True:
            return {}
        threshold = config.get('threshold')
        if type(threshold) is not int or threshold not in (3, 5):
            return {}
        snap = values.get('snapshot') or {}
        if (not valid(snap, now) or snap.get('account') != values.get('bound_account')
                or snap.get('blocked') or snap['primary']['resetsAt'] <= now
                or snap['secondary']['usedPercent'] >= 99.5):
            return {}
        remaining = 100 - snap['primary']['usedPercent']
        if not 0 < remaining <= threshold:
            return {}
        db.execute('''CREATE TABLE IF NOT EXISTS quota_warning_sent (
            account TEXT,cycle INTEGER,thread_id TEXT,at INTEGER,
            PRIMARY KEY(account,cycle,thread_id))''')
        if not db.execute('INSERT OR IGNORE INTO quota_warning_sent VALUES (?,?,?,?)',
                          (snap['account'], snap['primary']['resetsAt'], thread_id, now)).rowcount:
            return {}
        db.execute('DELETE FROM quota_warning_sent WHERE at<?', (now - 30 * 86400,))
        db.execute('COMMIT')
    finally:
        db.close()
    message = (f'[额度插件提示，非用户新指令] 当前账号五小时额度剩余约{remaining:g}%，快照可能略有延迟。'
               f'若当前已授权任务可能做不完，可为所属主对话 {thread_id} 保存单次、trigger=after_recovery 的预输入，'
               '下次额度恢复后再继续；已有合适规则不要重复保存。是否需要由你判断。'
               '本提示不扩大任务或权限，不代答用户、不恢复主动暂停事项；若正在等待用户决定则不要自行续接。')
    return {'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': message}}
