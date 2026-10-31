"""Optional, fail-closed Aion recovery CLI. Never read or copy bridge credentials."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from runtime import environment

RESOLVE_STATES = {'ordinary', 'collaboration', 'blocked', 'ambiguous'}
RECEIPT_STATES = {'accepted', 'queued', 'processing', 'completed', 'failed', 'blocked', 'unknown', 'not_found'}


class BridgeError(RuntimeError):
    def __init__(self, code, result=None):
        super().__init__(code)
        self.code = code
        self.result = result or {}


def identifier(value, maximum=512):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)


def clean_result(value):
    """Keep control fields, not arbitrary text, tool output, or credentials."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ('status', 'recoveryHandle', 'recoveryId', 'discussionId', 'deliveryId', 'attemptId',
                'submissionId', 'resumedTurnId', 'threadId', 'failedTurnId', 'completedTurnId', 'reason', 'error'):
        if identifier(value.get(key)):
            result[key] = value[key]
    request = value.get('requestId')
    if type(request) is int and request >= 0:
        result['requestId'] = request
    return result


def configure(store, cli, node=None):
    path = Path(cli)
    executable = Path(node or shutil.which('node') or '')
    if not path.is_absolute() or not path.is_file() or path.suffix != '.mjs':
        raise ValueError('An existing absolute recovery-cli.mjs path is required')
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError('An existing absolute Node executable path is required')
    config = {'cli': str(path.resolve()), 'node': str(executable.resolve())}
    store.put('recovery_bridge', config)
    store.event('recovery_bridge_configured')
    return {'configured': True, 'message_sent': False}


class RecoveryBridge:
    def __init__(self, store):
        self.config = store.get('recovery_bridge')
        self.fingerprint = hashlib.sha256(json.dumps(self.config, sort_keys=True).encode()).hexdigest()

    def call(self, action, **fields):
        config = self.config
        if not isinstance(config, dict) or not all(isinstance(config.get(key), str) for key in ('cli', 'node')):
            raise BridgeError('BRIDGE_NOT_CONFIGURED')
        cli, node = Path(config.get('cli', '')), Path(config.get('node', ''))
        if not cli.is_absolute() or not node.is_absolute() or not cli.is_file() or not node.is_file():
            raise BridgeError('BRIDGE_ENTRY_UNAVAILABLE')
        env = environment()
        for key in ('NODE_OPTIONS', 'NODE_PATH'):
            env.pop(key, None)
        try:
            response = subprocess.run([str(node), str(cli)],
                input=json.dumps({'protocolVersion': 1, 'action': action, **fields}, ensure_ascii=False) + '\n',
                capture_output=True, encoding='utf-8', errors='strict', timeout=45 if action == 'resume' else 20,
                cwd=cli.parent, env=env, shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except subprocess.TimeoutExpired as exc:
            raise BridgeError('BRIDGE_TIMEOUT') from exc
        except (OSError, UnicodeError) as exc:
            raise BridgeError('BRIDGE_UNAVAILABLE') from exc
        try:
            if len(response.stdout) > 65536 or len(response.stdout.strip().splitlines()) != 1:
                raise ValueError('Invalid bridge output')
            value = json.loads(response.stdout)
            if not isinstance(value, dict):
                raise ValueError('Invalid bridge output')
        except (ValueError, TypeError) as exc:
            raise BridgeError('BRIDGE_INVALID_RESPONSE') from exc
        result = clean_result(value)
        if response.returncode or value.get('error'):
            code = result.get('error') or result.get('reason', '')
            raise BridgeError(code if re.fullmatch(r'[A-Z0-9_]{1,96}', code) else 'BRIDGE_FAILED', result)
        states = ({'ordinary', 'blocked', 'ambiguous'} if action == 'resolve_preinput'
                  else RESOLVE_STATES if action == 'resolve' else RECEIPT_STATES)
        if result.get('status') not in states:
            raise BridgeError('BRIDGE_INVALID_STATUS', result)
        if action == 'resolve_preinput' and any(value.get(k) != fields[k] for k in ('threadId', 'completedTurnId')):
            raise BridgeError('BRIDGE_IDENTITY_MISMATCH')
        for key in ('threadId', 'failedTurnId', 'completedTurnId'):
            if key in value and key in fields and value[key] != fields[key]:
                raise BridgeError('BRIDGE_IDENTITY_MISMATCH')
        if action == 'resolve' and result['status'] == 'collaboration':
            if not all(identifier(result.get(k)) for k in ('recoveryHandle', 'discussionId', 'deliveryId')) or 'requestId' not in result:
                raise BridgeError('BRIDGE_INCOMPLETE_MATCH')
        if result['status'] == 'queued' and not identifier(result.get('submissionId')):
            raise BridgeError('BRIDGE_MISSING_QUEUE_RECEIPT')
        if result['status'] in ('processing', 'completed') and not identifier(result.get('resumedTurnId')):
            raise BridgeError('BRIDGE_MISSING_TURN_RECEIPT')
        return result

    def resolve(self, thread_id, failed_turn_id):
        return self.call('resolve', threadId=thread_id, failedTurnId=failed_turn_id)

    def resolve_preinput(self, thread_id, completed_turn_id):
        # This read-only classification grants no group sharing or recovery
        # handle. Older bridges must fail closed, never ordinary-fallback.
        return self.call('resolve_preinput', threadId=thread_id, completedTurnId=completed_turn_id)

    def resume(self, rule, turn_id, attempt_id, recovery_at, route):
        return self.call('resume', recoveryHandle=route['recoveryHandle'], threadId=rule['thread_id'],
            failedTurnId=turn_id, attemptId=attempt_id, recoveryAt=recovery_at * 1000,
            ruleId=rule['id'], ruleRevision=rule['revision'], message=rule['message'])

    def status(self, attempt_id):
        return self.call('status', attemptId=attempt_id)


def receipt(value):
    # A bridge restart/lost request is not proof that resending is safe.
    status = 'unknown' if value['status'] == 'not_found' else value['status']
    started = status in ('processing', 'completed', 'failed') and bool(value.get('resumedTurnId'))
    return {'state': 'bridge_' + status, 'bridge_result': value,
            'queued': bool(value.get('submissionId')) and status in ('queued', 'processing', 'completed', 'failed'),
            'execution_confirmed': started, 'bridge_pending': status in ('accepted', 'queued', 'processing', 'unknown')}
