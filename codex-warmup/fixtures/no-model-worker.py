"""Lifecycle test fixture: real process/SQLite/lease, no Codex or network calls."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('command', choices=['worker'])
    args = parser.parse_args()
    engine.tick = lambda store: 'lifecycle_fixture_only'
    engine.worker(engine.Store(args.data_dir))
