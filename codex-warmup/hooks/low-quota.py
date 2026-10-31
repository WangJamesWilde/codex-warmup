"""Informational hook only: never block/approve/rewrite a tool or start a turn."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quota_warning import warning

if __name__ == '__main__':
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        raw = sys.stdin.read(262145)
        value = warning(json.loads(raw), caller_thread_id=os.environ.get('CODEX_THREAD_ID')) if len(raw) <= 262144 else {}
    except Exception:
        # Missing/stale state, lock contention or malformed hook input must not
        # interrupt the user's tool or leak its inputs through an error message.
        value = {}
    print(json.dumps(value, ensure_ascii=False))
