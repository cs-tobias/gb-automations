"""TTY helpers: prompts, browser stops, polling spinners.

Kept dependency-free (stdlib only) so the installer can run before `uv sync`.
ANSI colors degrade gracefully on dumb terminals.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable
from typing import TypeVar

_T = TypeVar("_T")

_USE_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def heading(text: str) -> None:
    bar = "=" * min(78, max(20, len(text)))
    print()
    print(_c("1;36", bar))
    print(_c("1;36", text))
    print(_c("1;36", bar))


def step(num: int, total: int, text: str) -> None:
    print()
    print(_c("1;34", f"[{num}/{total}] {text}"))


def info(text: str) -> None:
    print(f"  {text}")


def ok(text: str) -> None:
    print(f"  {_c('32', '✓')} {text}")


def warn(text: str) -> None:
    print(f"  {_c('33', '⚠')}  {text}", file=sys.stderr)


def err(text: str) -> None:
    print(f"  {_c('31', '✗')} {text}", file=sys.stderr)


def die(text: str, exit_code: int = 1) -> None:
    err(text)
    sys.exit(exit_code)


def ask(prompt: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    if secret:
        import getpass

        while True:
            value = getpass.getpass(f"  {prompt}{suffix}: ").strip()
            if value:
                return value
            if default is not None:
                return default
            err("Value required.")
    while True:
        value = input(f"  {prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        err("Value required.")


def confirm(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"  {prompt} {suffix} ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


def copy_to_clipboard(text: str) -> bool:
    """macOS-only via pbcopy. Best-effort; returns False if unavailable."""
    pbcopy = shutil.which("pbcopy")
    if not pbcopy:
        return False
    try:
        subprocess.run([pbcopy], input=text, text=True, check=True, timeout=2)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def browser_stop(
    title: str,
    url: str,
    instructions: list[str],
    clipboard: str | None = None,
    *,
    open_browser: bool = True,
) -> None:
    """Print a browser-stop block, optionally open the URL, copy a payload.

    The caller is responsible for the actual verification (poll an API after
    this returns). We just prepare the UX.
    """
    print()
    print(_c("1;33", f"  ⚐ BROWSER STOP — {title}"))
    print()
    print(f"    URL: {_c('4', url)}")
    print()
    for i, line in enumerate(instructions, start=1):
        print(f"    {i}. {line}")
    print()

    if clipboard:
        if copy_to_clipboard(clipboard):
            ok(f"Copied to clipboard: {_truncate(clipboard, 60)}")
        else:
            info(f"Copy manually: {clipboard}")

    if open_browser:
        try:
            webbrowser.open(url, new=2)
        except webbrowser.Error:
            pass


def wait_for(
    description: str,
    probe: Callable[[], bool],
    *,
    interval_s: float = 3.0,
    timeout_s: float = 600.0,
) -> bool:
    """Poll until `probe()` returns truthy or timeout elapses.

    Prints a spinner. The probe must be cheap (a single API call) — we call
    it once per `interval_s`. Returns True on success, False on timeout.
    """
    spinner = "|/-\\"
    deadline = time.monotonic() + timeout_s
    i = 0
    last_print = 0.0
    while time.monotonic() < deadline:
        try:
            if probe():
                if _USE_COLOR:
                    sys.stdout.write("\r" + " " * 80 + "\r")
                    sys.stdout.flush()
                ok(f"{description} — confirmed")
                return True
        except Exception as e:  # noqa: BLE001 — probes are best-effort
            # Print sparingly so we don't fill the terminal during long waits.
            if time.monotonic() - last_print > 30:
                warn(f"probe error (will keep trying): {type(e).__name__}: {e}")
                last_print = time.monotonic()
        if _USE_COLOR:
            sys.stdout.write(f"\r  {spinner[i % 4]} waiting: {description} ")
            sys.stdout.flush()
        time.sleep(interval_s)
        i += 1
    if _USE_COLOR:
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()
    err(f"{description} — timed out after {int(timeout_s)}s")
    return False


def press_enter(prompt: str = "Press Enter to continue") -> None:
    input(f"  → {prompt}…")


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."
