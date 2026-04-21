"""
Terminal UI helpers for the Memory Compiler console window.

Lifted from the show-compile-progress skill template and adapted as an
importable module: no subprocess spawn, no pipe streaming. compile.py
prints step messages directly; this module handles the banner, spinner,
and footer around them.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

SPINNER_FRAMES = "|/-\\"
BAR = "=" * 60


def enable_utf8_console() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass


def print_banner(
    project_name: str,
    last_compile_at: str | None = None,
    cooldown_hours: int | None = None,
) -> None:
    if last_compile_at:
        try:
            last_dt = datetime.fromisoformat(last_compile_at)
            since = last_dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            since = last_compile_at
        interval = f"compiling new entries since {since}"
    else:
        interval = "compiling pending daily log entries"
    suffix = f" ({cooldown_hours}h cooldown)" if cooldown_hours else ""
    print(BAR)
    print(f"  Memory Compiler - {interval}{suffix}")
    print(f"  project {project_name}")
    print(f"  started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(BAR)
    print()


def clear_spinner_line() -> None:
    # Write to the raw stdout so spinner control characters bypass any tee
    # that compile.py has installed on sys.stdout (otherwise \r frames would
    # land in compile.log and make it unreadable).
    sys.__stdout__.write("\r" + " " * 50 + "\r")
    sys.__stdout__.flush()


def _spin(stop_event: threading.Event, start_time: float) -> None:
    frames = itertools.cycle(SPINNER_FRAMES)
    while not stop_event.is_set():
        elapsed = time.monotonic() - start_time
        frame = next(frames)
        sys.__stdout__.write(f"\r  [{frame}] working... {elapsed:5.1f}s ")
        sys.__stdout__.flush()
        time.sleep(0.1)
    clear_spinner_line()


def start_spinner(start_time: float) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()
    thread = threading.Thread(target=_spin, args=(stop_event, start_time), daemon=True)
    thread.start()
    return stop_event, thread


def print_footer(
    exit_code: int,
    duration_seconds: float,
    log_file: Path | None = None,
) -> None:
    print()
    print(BAR)
    if exit_code == 0:
        print(f"  [ok] compile complete in {duration_seconds:.1f}s")
    else:
        print(f"  [err] compile failed (exit {exit_code}) after {duration_seconds:.1f}s")
        if log_file is not None:
            print(f"        see {log_file} for details")
    print(BAR)
