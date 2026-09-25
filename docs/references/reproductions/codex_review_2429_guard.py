"""Run isolated review tests with file/network guardrails before app imports."""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys

review_root = Path('/Users/inkant/codex-review-20260924').resolve()
source_root = Path('/Users/inkant/learn-ai').resolve()
cwd = Path.cwd().resolve()
if not cwd.is_relative_to(review_root):
    raise SystemExit('Tests must execute inside an isolated review directory')

def guard(event: str, args: tuple[object, ...]) -> None:
    if event in {'socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.bind'}:
        raise PermissionError(f'Review prohibits network event: {event}')
    if event in {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'}:
        raise PermissionError(f'Review prohibits unguarded child processes: {event}')
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.name == '.env' or path.name.startswith('.env.') or 'live_runs' in path.parts or '/docs/audits/' in str(path):
            raise PermissionError(f'Review prohibits protected file: {path}')
        if path.is_relative_to(source_root) and not path.is_relative_to(source_root/'PythonDataService/.venv'):
            raise PermissionError(f'Review tests must not read the running checkout: {path}')
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writing = isinstance(mode, str) and any(char in mode for char in 'wax+')
        writing |= isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if writing and not path.is_relative_to(review_root) and str(path) not in {'/dev/null', '/dev/tty'}:
            raise PermissionError(f'Review prohibits writes outside review directory: {path}')

sys.addaudithook(guard)
sys.dont_write_bytecode = True
sys.path.insert(0, str(cwd))
if sys.argv[1] == '-m':
    module = sys.argv[2]
    sys.argv = [module, *sys.argv[3:]]
    runpy.run_module(module, run_name='__main__')
else:
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(script, run_name='__main__')
