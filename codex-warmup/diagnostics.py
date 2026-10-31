"""Bounded local diagnostics; never persist raw exceptions, prompts or credentials."""
import ctypes
import json
from logging import LogRecord
from logging.handlers import RotatingFileHandler
import os
import time


def failure(exc, stage):
    value = {'at': int(time.time()), 'pid': os.getpid(), 'stage': stage, 'type': type(exc).__name__}
    for key in ('winerror', 'errno', 'rpc_code'):
        code = getattr(exc, key, None)
        if type(code) is int:
            value[key] = code
    method = getattr(exc, 'rpc_method', None)
    if isinstance(method, str) and len(method) <= 80 and all(c.isalnum() or c in '/_' for c in method):
        value['method'] = method
    return value


def record(store, event, value, key=None):
    item = {'at': int(time.time()), 'pid': os.getpid(), 'event': event, **value}
    # The file remains useful if SQLite itself is temporarily inaccessible.
    try:
        handler = RotatingFileHandler(store.root / 'diagnostics.jsonl', maxBytes=262144, backupCount=2, encoding='utf-8')
        try:
            handler.emit(LogRecord('warmup', 20, '', 0, json.dumps(item), (), None))
        finally:
            handler.close()
    except OSError:
        pass
    try:
        if key:
            store.put(key, item)
        store.event(event, json.dumps(value))
    except Exception:
        pass


def process_alive(pid):
    if type(pid) is not int or pid <= 0 or os.name != 'nt':
        return None
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        code = ctypes.c_uint32()
        return code.value == 259 if kernel.GetExitCodeProcess(handle, ctypes.byref(code)) else None
    finally:
        kernel.CloseHandle(handle)


def health(store, now=None):
    now = time.time() if now is None else now
    worker = store.get('worker') or {}
    age = max(0, now - worker['heartbeat']) if type(worker.get('heartbeat')) in (int, float) else None
    alive = process_alive(worker.get('pid'))
    launch = store.get('worker_last_launch') or {}
    if not store.get('enabled', False):
        state = 'paused'
    elif worker.get('pid') != launch.get('worker_pid') and 0 <= now - launch.get('at', 0) < 15:
        state = 'starting'
    elif alive is False:
        state = 'process_exited'
    elif worker.get('state') == 'stopped':
        state = 'stopped'
    elif age is None:
        state = 'missing'
    elif age > 180:
        state = 'stale'
    else:
        state = 'healthy'
    return {'state': state, 'process_alive': alive, 'heartbeat_age_seconds': round(age, 1) if age is not None else None,
            'last_launch': launch or None, 'last_start': store.get('worker_last_start'),
            'last_exit': store.get('worker_last_exit'), 'last_error': store.get('worker_last_error'),
            'log_path': str(store.root / 'diagnostics.jsonl')}
