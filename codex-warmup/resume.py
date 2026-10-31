"""Explicitly selected chat rules; never execute or approve another chat's tools."""
import json
import time
import uuid

from runtime import Rpc, desktop_running, queue_message
from collaboration import BridgeError, RecoveryBridge, receipt


def setup(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS resume_rules (
            id TEXT PRIMARY KEY, account TEXT NOT NULL, thread_id TEXT NOT NULL,
            title TEXT NOT NULL, message TEXT NOT NULL, repeat INTEGER NOT NULL,
            enabled INTEGER NOT NULL, armed_at INTEGER NOT NULL, revision INTEGER NOT NULL,
            last_result TEXT, run_limit INTEGER, remaining_runs INTEGER, UNIQUE(account,thread_id));
        CREATE TABLE IF NOT EXISTS resume_attempts (
            rule_id TEXT NOT NULL, turn_id TEXT NOT NULL, recovery_at INTEGER NOT NULL,
            at INTEGER NOT NULL, result TEXT NOT NULL, PRIMARY KEY(rule_id,turn_id));
        CREATE TABLE IF NOT EXISTS resume_intents (
            rule_id TEXT PRIMARY KEY, trigger TEXT NOT NULL, anchor_turn_id TEXT);
        CREATE TABLE IF NOT EXISTS resume_recovery_claims (
            rule_id TEXT NOT NULL, recovery_at INTEGER NOT NULL, PRIMARY KEY(rule_id,recovery_at));
    ''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(resume_rules)')}
    if 'run_limit' not in columns or 'remaining_runs' not in columns:
        db.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in db.execute('PRAGMA table_info(resume_rules)')}
        migrated = False
        for column in ('run_limit', 'remaining_runs'):
            if column not in columns:
                db.execute('ALTER TABLE resume_rules ADD COLUMN ' + column + ' INTEGER')
                migrated = True
        if migrated:
            db.execute('''UPDATE resume_rules SET run_limit=CASE WHEN repeat=1 THEN NULL ELSE 1 END,
                remaining_runs=CASE WHEN repeat=1 THEN NULL WHEN last_result IS NOT NULL
                OR EXISTS(SELECT 1 FROM resume_attempts WHERE rule_id=resume_rules.id) THEN 0 ELSE 1 END''')
        db.execute('COMMIT')


def rules(store):
    with store.db() as db:
        setup(db)
        rows = db.execute('SELECT id,account,thread_id,title,message,repeat,enabled,armed_at,revision,last_result,run_limit,remaining_runs FROM resume_rules ORDER BY armed_at,id').fetchall()
        intents = {rid: (trigger, anchor) for rid, trigger, anchor in db.execute('SELECT rule_id,trigger,anchor_turn_id FROM resume_intents')}
    return [dict(id=r[0], account=r[1], thread_id=r[2], title=r[3], message=r[4],
                 repeat=bool(r[5]), enabled=bool(r[6]), armed_at=r[7], revision=r[8],
                 last_result=json.loads(r[9]) if r[9] else None,run_limit=r[10],remaining_runs=r[11],
                 trigger=intents.get(r[0], ('quota_stop', None))[0],
                 anchor_turn_id=intents.get(r[0], ('quota_stop', None))[1]) for r in rows]


def run_limit(data):
    value = data['run_limit'] if 'run_limit' in data else (None if data.get('repeat', False) else 1)
    if value is not None and (type(value) is not int or not 1 <= value <= 1000):
        raise ValueError('Run count must be an integer from 1 to 1000')
    return value


def save_rule(store, data, rpc_factory=Rpc):
    if data.get('confirm_send') is not True:
        raise ValueError('Explicit permission to send this message is required')
    thread_id = str(uuid.UUID(str(data.get('thread_id', ''))))
    message = data.get('message')
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 4000:
        raise ValueError('Message must contain 1 to 4000 characters')
    if type(data.get('repeat', False)) is not bool or type(data.get('enabled', True)) is not bool:
        raise ValueError('Rule switches must be boolean')
    limit = run_limit(data)
    old_rule = next((r for r in rules(store) if r['thread_id'] == thread_id and r['account'] == store.get('bound_account')), None)
    trigger = data.get('trigger', (old_rule or {}).get('trigger', 'quota_stop'))
    if trigger not in ('quota_stop', 'after_recovery'):
        raise ValueError('Unknown recovery trigger')
    anchor = None
    with rpc_factory(store.root / 'empty-workspace') as rpc:
        snap = rpc.snapshot()
        if snap.get('auth') != 'chatgpt' or snap['account'] != store.get('bound_account'):
            raise ValueError('Account changed; rule not saved')
        thread = rpc.call('thread/read', {'threadId': thread_id, 'includeTurns': False}).get('thread') or {}
        if trigger == 'after_recovery':
            anchor = capture_anchor(rpc, thread_id)
    if thread.get('ephemeral') or thread.get('parentThreadId') or not thread.get('model'):
        raise ValueError('Only existing main chats with a known model can be selected')
    title = thread.get('name') or '未命名对话'
    now = int(time.time())
    with store.db() as db:
        setup(db)
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT id,revision,run_limit,remaining_runs FROM resume_rules WHERE account=? AND thread_id=?', (snap['account'], thread_id)).fetchone()
        if 'expected_revision' in data and data['expected_revision'] != (old[1] if old else 0):
            db.execute('ROLLBACK')
            return {'conflict': True, 'message_sent': False}
        if not old and db.execute('SELECT COUNT(*) FROM resume_rules').fetchone()[0] >= 20:
            db.execute('ROLLBACK')
            raise ValueError('At most 20 rules')
        rid, revision = (old[0], old[1] + 1) if old else (str(uuid.uuid4()), 1)
        remaining = old[3] if old and old[2] == limit and old[3] else limit
        db.execute('INSERT OR REPLACE INTO resume_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (rid, snap['account'], thread_id, title, message.strip(), int(limit != 1),
                    int(data.get('enabled', True)), now, revision, None, limit, remaining))
        db.execute('INSERT OR REPLACE INTO resume_intents VALUES (?,?,?)', (rid, trigger, anchor))
        db.execute('COMMIT')
    store.event('resume_rule_saved', title)
    return {'rules': rules(store), 'message_sent': False}


def remove_rule(store, rid):
    # The UI calls this only after the user selects removal; keep the send ledger.
    uuid.UUID(str(rid))
    with store.db() as db:
        setup(db)
        db.execute('DELETE FROM resume_rules WHERE id=?', (rid,))
        db.execute('DELETE FROM resume_intents WHERE rule_id=?', (rid,))
    return {'rules': rules(store), 'message_sent': False}


def set_rule_enabled(store, data, rpc_factory=Rpc):
    """Set a desired state, rather than blindly toggling a possibly stale row."""
    thread_id = str(uuid.UUID(str(data.get('thread_id', ''))))
    enabled, expected = data.get('enabled'), data.get('expected_revision')
    if type(enabled) is not bool or type(expected) is not int or expected < 0:
        raise ValueError('A desired state and nonnegative revision are required')
    if enabled and data.get('confirm_send') is not True:
        raise ValueError('Explicit message-sending consent is required')
    account = store.get('bound_account')
    if not account:
        raise ValueError('No verified account binding')
    title = None
    old_intent = next((r for r in rules(store) if r['thread_id'] == thread_id and r['account'] == account), None)
    anchor = None
    if enabled:
        with rpc_factory(store.root / 'empty-workspace') as rpc:
            snap = rpc.snapshot()
            if snap.get('auth') != 'chatgpt' or snap.get('account') != account:
                raise ValueError('Account changed; rule not enabled')
            thread = rpc.call('thread/read', {'threadId': thread_id, 'includeTurns': False}).get('thread') or {}
            if (old_intent or {}).get('trigger') == 'after_recovery':
                anchor = capture_anchor(rpc, thread_id)
        if thread.get('ephemeral') or thread.get('parentThreadId') or not thread.get('model'):
            raise ValueError('Only existing main chats with a known model can be selected')
        title = thread.get('name') or '未命名对话'
    now = int(time.time())
    with store.db() as db:
        setup(db)
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT id,title,enabled,revision FROM resume_rules WHERE account=? AND thread_id=?',
                         (account, thread_id)).fetchone()
        if (old[3] if old else 0) != expected:
            db.execute('ROLLBACK')
            return {'conflict': True, 'message_sent': False}
        if old:
            if enabled and old[2]:
                db.execute('COMMIT')
                return {'conflict': False, 'message_sent': False}
            # Preserve the edited message/repeat setting. Cancellation increments
            # revision even after a single-use send has reserved its disabled row.
            if enabled:
                db.execute('''UPDATE resume_rules SET enabled=1,armed_at=?,revision=revision+1,last_result=NULL,title=?,
                    remaining_runs=CASE WHEN run_limit IS NULL THEN NULL WHEN remaining_runs<=0 THEN run_limit ELSE remaining_runs END WHERE id=?''',
                           (now, title, old[0]))
                if anchor is not None:
                    db.execute('UPDATE resume_intents SET anchor_turn_id=? WHERE rule_id=?', (anchor, old[0]))
            else:
                db.execute('UPDATE resume_rules SET enabled=0,revision=revision+1 WHERE id=?', (old[0],))
            title = title or old[1]
        elif enabled:
            if db.execute('SELECT COUNT(*) FROM resume_rules').fetchone()[0] >= 20:
                db.execute('ROLLBACK')
                raise ValueError('At most 20 rules')
            db.execute('INSERT INTO resume_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (str(uuid.uuid4()), account, thread_id, title, '继续', 0, 1, now, 1, None, 1, 1))
        db.execute('COMMIT')
    if title:
        store.event('resume_rule_armed' if enabled else 'resume_rule_disabled', title)
    return {'conflict': False, 'message_sent': False}


def set_rule_mode(store, data):
    rid = str(uuid.UUID(str(data.get('rule_id', ''))))
    expected = data.get('expected_revision')
    if type(expected) is not int or expected < 1 or data.get('confirm_send') is not True:
        raise ValueError('Explicit mode selection and current revision are required')
    limit = run_limit(data)
    now = int(time.time())
    with store.db() as db:
        setup(db)
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT account,revision,title FROM resume_rules WHERE id=?', (rid,)).fetchone()
        if not old or old[1] != expected:
            db.execute('ROLLBACK')
            return {'conflict': True, 'message_sent': False}
        if old[0] != store.get('bound_account'):
            db.execute('ROLLBACK')
            raise ValueError('Account binding does not match this rule')
        db.execute('''UPDATE resume_rules SET run_limit=?,remaining_runs=?,repeat=?,armed_at=?,
            revision=revision+1,last_result=NULL WHERE id=?''', (limit, limit, int(limit != 1), now, rid))
        db.execute('COMMIT')
    store.event('resume_rule_mode_changed', old[2])
    return {'conflict': False, 'message_sent': False}


def observed_recovery(snap, previous, next_reset, now):
    from engine import valid
    if not valid(snap, now) or snap.get('blocked'):
        return None
    if any(snap[k]['usedPercent'] >= 99.5 for k in ('primary', 'secondary')):
        return None
    if not previous or not valid(previous, previous.get('at', now)) or previous.get('account') != snap['account']:
        return None
    old, new = previous['primary'], snap['primary']
    was_blocked = previous.get('blocked') or old.get('usedPercent', 0) >= 99.5 or (previous.get('secondary') or {}).get('usedPercent', 0) >= 99.5
    changed = old.get('resetsAt') != new['resetsAt']
    if was_blocked or (changed and (new['usedPercent'] < old.get('usedPercent', 0) or old.get('resetsAt', now + 1) <= now)):
        return {'at': now, 'reset': new['resetsAt'], 'account': snap['account']}
    if next_reset and next_reset <= now and new['resetsAt'] > now:
        return {'at': now, 'reset': new['resetsAt'], 'account': snap['account']}
    return None


def quota_stop(rpc, thread_id, recovery_at):
    thread = rpc.call('thread/read', {'threadId': thread_id, 'includeTurns': False}).get('thread') or {}
    if (thread.get('status') or {}).get('type') not in ('idle', 'notLoaded'):
        return None, 'chat_busy_or_unknown'
    page = rpc.call('thread/turns/list', {'threadId': thread_id, 'limit': 1, 'sortDirection': 'desc'})
    turns = page.get('data') or []
    if not turns:
        return None, 'no_quota_stop_evidence'
    turn = turns[0]
    # A stopped/in-progress desktop turn can appear as interrupted in a separate
    # metadata client. Never infer a quota stop from status or error prose alone.
    if (turn.get('status') not in ('failed', 'interrupted')
            or (turn.get('error') or {}).get('codexErrorInfo') != 'usageLimitExceeded'
            or type(turn.get('completedAt')) is not int
            or turn['completedAt'] > recovery_at):
        return None, 'not_proven_quota_stop'
    if not thread.get('model') or not turn.get('id'):
        return None, 'chat_metadata_unknown'
    goal = rpc.call('thread/goal/get', {'threadId': thread_id}).get('goal')
    if goal and goal.get('status') not in ('active', 'usageLimited'):
        return None, 'goal_paused_or_not_actionable'
    return turn['id'], 'eligible'


def capture_anchor(rpc, thread_id):
    page = rpc.call('thread/turns/list', {'threadId': thread_id, 'limit': 1, 'sortDirection': 'desc'})
    turn = (page.get('data') or [{}])[0]
    anchor = turn.get('id')
    if not isinstance(anchor, str) or not anchor or len(anchor) > 160:
        raise ValueError('A known current turn is required for a preinput')
    return anchor


def resume_candidate(rpc, rule, recovery_at):
    if rule.get('trigger', 'quota_stop') == 'quota_stop':
        return quota_stop(rpc, rule['thread_id'], recovery_at)
    if rule.get('trigger') != 'after_recovery':
        return None, 'rule_trigger_unknown'
    thread = rpc.call('thread/read', {'threadId': rule['thread_id'], 'includeTurns': False}).get('thread') or {}
    status = thread.get('status') or {}
    if status.get('type') not in ('idle', 'notLoaded') or status.get('activeFlags') or thread.get('archived'):
        return None, 'chat_busy_or_unknown'
    page = rpc.call('thread/turns/list', {'threadId': rule['thread_id'], 'limit': 1, 'sortDirection': 'desc'})
    turn = (page.get('data') or [{}])[0]
    if not turn.get('id') or not thread.get('model') or type(turn.get('completedAt')) is not int:
        return None, 'chat_metadata_unknown'
    if turn['completedAt'] > recovery_at:
        return None, 'chat_progressed_after_recovery'
    # A one-shot preinput belongs to the exact turn the user selected. New
    # manual work invalidates that snapshot instead of reviving an older plan.
    if rule['run_limit'] == 1 and turn['id'] != rule.get('anchor_turn_id'):
        return None, 'preinput_anchor_changed'
    quota_failed = (turn.get('status') in ('failed', 'interrupted')
                    and (turn.get('error') or {}).get('codexErrorInfo') == 'usageLimitExceeded')
    completed = turn.get('status') == 'completed' and not turn.get('error')
    if not quota_failed and not completed:
        return None, 'not_safe_preinput_target'
    goal = rpc.call('thread/goal/get', {'threadId': rule['thread_id']}).get('goal')
    if goal and goal.get('status') not in ('active', 'usageLimited'):
        return None, 'goal_paused_or_not_actionable'
    return turn['id'], 'eligible' if quota_failed else 'explicit_completed'


def note_rule(store, rule, result):
    with store.db() as db:
        updated = db.execute('UPDATE resume_rules SET last_result=? WHERE id=? AND revision=?',
                            (json.dumps(result), rule['id'], rule['revision'])).rowcount
    if updated and (rule.get('last_result') or {}).get('state') != result['state']:
        store.event('resume_' + result['state'], rule['title'])


def note_skip(store, selected, reason, now):
    checks = store.get('resume_diagnostics', {})
    live = {rule['id'] for rule in rules(store)}
    checks = {key: value for key, value in checks.items() if key in live}
    for rule in selected:
        checks[rule['id']] = {'at': now, 'reason': reason}
    store.put('resume_diagnostics', checks)


def reconcile_bridge(store, bridge, now):
    """Read receipts only. A lost response never becomes permission to send again."""
    with store.db() as db:
        setup(db)
        rows = db.execute('SELECT rule_id,turn_id,result FROM resume_attempts').fetchall()
    for rule_id, turn_id, raw in rows:
        try:
            result = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(result, dict) or not result.get('bridge_pending'):
            continue
        if (result.get('bridge_fingerprint') != bridge.fingerprint
                or result.get('bridge_account') != store.get('bound_account')
                or now - result.get('bridge_checked_at', result['at']) < 60):
            continue
        updated = {**result, 'bridge_checked_at': now}
        try:
            updated.update(receipt(bridge.status(result['attempt_id'])))
        except BridgeError:
            # Keep the last confirmed state, but throttle metadata retries.
            # Elapsed time never authorizes resending or ends receipt tracking.
            pass
        encoded = json.dumps(updated)
        with store.db() as db:
            if not db.execute('UPDATE resume_attempts SET result=? WHERE rule_id=? AND turn_id=? AND result=?',
                              (encoded, rule_id, turn_id, raw)).rowcount:
                continue
            db.execute('UPDATE resume_rules SET last_result=? WHERE id=? AND revision=?',
                       (encoded, rule_id, result['rule_revision']))


def dispatch(store, rpc, snap, now, sender=None, is_desktop_running=None, bridge_factory=None):
    sender = sender or queue_message
    is_desktop_running = is_desktop_running or desktop_running
    bridge = (bridge_factory or RecoveryBridge)(store) if store.get('recovery_bridge') is not None else None
    if bridge:
        reconcile_bridge(store, bridge, now)
    active = [rule for rule in rules(store) if rule['enabled'] and rule['account'] == snap['account']]
    recovery = store.get('resume_recovery')
    if not recovery or recovery.get('account') != snap['account']:
        note_skip(store, active, 'waiting_recovery_evidence', now)
        return
    note_skip(store, [rule for rule in active if rule['armed_at'] >= recovery['at']], 'waiting_next_recovery', now)
    selected = [rule for rule in active if rule['armed_at'] < recovery['at']]
    if not selected:
        return
    if not is_desktop_running():
        note_skip(store, selected, 'desktop_not_running', now)
        return
    from engine import valid
    if not valid(snap, now) or snap.get('blocked') or any(snap[k]['usedPercent'] >= 99.5 for k in ('primary', 'secondary')):
        note_skip(store, selected, 'quota_unavailable', now)
        return
    for rule in selected:
        if not store.get('enabled', False):
            return
        if not rule['enabled'] or rule['account'] != snap['account'] or rule['armed_at'] >= recovery['at']:
            continue
        turn_id, reason = resume_candidate(rpc, rule, recovery['at'])
        if not turn_id:
            note_skip(store, [rule], reason, now)
            continue
        with store.db() as db:
            if rule.get('trigger') == 'after_recovery' and db.execute(
                    'SELECT 1 FROM resume_recovery_claims WHERE rule_id=? AND recovery_at=?', (rule['id'], recovery['at'])).fetchone():
                note_skip(store, [rule], 'already_attempted', now)
                continue
            if db.execute('SELECT 1 FROM resume_attempts WHERE rule_id=? AND turn_id=?', (rule['id'], turn_id)).fetchone():
                note_skip(store, [rule], 'already_attempted', now)
                continue
        route = {'status': 'ordinary'}
        if bridge:
            try:
                route = (bridge.resolve_preinput(rule['thread_id'], turn_id) if reason == 'explicit_completed'
                         else bridge.resolve(rule['thread_id'], turn_id))
            except BridgeError as exc:
                note_rule(store, rule, {'at': now, 'state': 'bridge_unavailable', 'queued': False,
                                       'bridge_error': exc.code, 'bridge_result': exc.result})
                continue
            if route['status'] not in ('ordinary', 'collaboration'):
                note_rule(store, rule, {'at': now, 'state': 'bridge_' + route['status'], 'queued': False,
                                       'bridge_result': route})
                continue
        attempt_id = str(uuid.uuid4())
        reserved = {'at': now, 'state': 'reserved_no_retry', 'attempt_id': attempt_id,
                    'rule_revision': rule['revision'], 'queued': False}
        if route['status'] == 'collaboration':
            reserved.update(bridge_pending=True, bridge_checked_at=now,
                            bridge_account=snap['account'], bridge_fingerprint=bridge.fingerprint)
        with store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT enabled,revision,remaining_runs FROM resume_rules WHERE id=?', (rule['id'],)).fetchone()
            if not current or current[:2] != (1, rule['revision']) or (current[2] is not None and current[2] <= 0):
                db.execute('ROLLBACK')
                continue
            if not db.execute('INSERT OR IGNORE INTO resume_attempts VALUES (?,?,?,?,?)',
                              (rule['id'], turn_id, recovery['at'], now, json.dumps(reserved))).rowcount:
                db.execute('ROLLBACK')
                continue
            if rule.get('trigger') == 'after_recovery' and not db.execute(
                    'INSERT OR IGNORE INTO resume_recovery_claims VALUES (?,?)', (rule['id'], recovery['at'])).rowcount:
                db.execute('ROLLBACK')
                continue
            # Single-use consent is consumed before transport, including an
            # ambiguous timeout. Re-arming never retries the same quota turn.
            if current[2] is not None:
                db.execute('UPDATE resume_rules SET remaining_runs=remaining_runs-1,enabled=? WHERE id=?',
                           (int(current[2] > 1), rule['id']))
            db.execute('COMMIT')
        try:
            fresh_id, fresh_reason = resume_candidate(rpc, rule, recovery['at'])
            if fresh_id != turn_id or fresh_reason != reason or not store.get('enabled', False):
                result = {'at': now, 'state': 'cancelled_state_changed', 'queued': False}
            else:
                fresh = rpc.snapshot()
                safe = (fresh.get('account') == snap['account'] and fresh.get('account') == store.get('bound_account')
                        and valid(fresh, fresh.get('at', now)) and not fresh.get('blocked')
                        and all(fresh[k]['usedPercent'] < 99.5 for k in ('primary', 'secondary')))
                with store.db() as db:
                    current = db.execute('SELECT revision FROM resume_rules WHERE id=?', (rule['id'],)).fetchone()
                safe = (safe and current == (rule['revision'],) and store.get('enabled', False)
                        and (not bridge or store.get('recovery_bridge') == bridge.config))
                if not safe:
                    result = {'at': now, 'state': 'cancelled_state_changed', 'queued': False}
                else:
                    if route['status'] == 'collaboration':
                        result = {**reserved, **receipt(bridge.resume(rule, turn_id, attempt_id, recovery['at'], route))}
                    else:
                        result = {'at': now, **sender(rule['thread_id'], rule['message'])}
        except BridgeError as exc:
            result = {**reserved, 'state': 'bridge_unknown', 'bridge_error': exc.code,
                      'bridge_result': exc.result, 'execution_confirmed': False}
        except Exception as exc:
            result = {**reserved, 'state': 'bridge_unknown' if bridge and route['status'] == 'collaboration' else 'unconfirmed_no_retry',
                      'queued': False, 'error': type(exc).__name__}
        with store.db() as db:
            db.execute('UPDATE resume_attempts SET result=? WHERE rule_id=? AND turn_id=?', (json.dumps(result), rule['id'], turn_id))
        note_rule(store, rule, result)
