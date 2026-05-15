"""Tests for _thread_lock_key — keys feed into pg_advisory_xact_lock to
serialize concurrent sync_thread runs on the same Gmail thread. Must be
deterministic across processes, otherwise the lock is useless under multiple
workers."""

import subprocess
import sys

from gb_automations.sync.sync_thread import _thread_lock_key


def test_lock_key_is_signed_int64():
    key = _thread_lock_key("abc123")
    assert isinstance(key, int)
    assert -(2**63) <= key < 2**63


def test_lock_key_stable_for_same_thread():
    assert _thread_lock_key("THREAD_ABC") == _thread_lock_key("THREAD_ABC")


def test_lock_key_differs_for_distinct_threads():
    assert _thread_lock_key("THREAD_A") != _thread_lock_key("THREAD_B")


def test_lock_key_stable_across_processes():
    # Would silently break multi-worker setups if we used Python's built-in
    # hash() (randomized per process via PYTHONHASHSEED).
    a = _thread_lock_key("THREAD_XYZ")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from gb_automations.sync.sync_thread import _thread_lock_key; "
            "print(_thread_lock_key('THREAD_XYZ'))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert int(proc.stdout.strip()) == a
