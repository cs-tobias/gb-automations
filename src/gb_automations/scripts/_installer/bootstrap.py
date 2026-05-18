"""Auto-installers for the three no-sudo CLI tools the installer depends on.

The installer's pre-flight (step 0) calls `ensure_tool(...)` for each. If the
tool is missing, we offer to install it; the operator accepts or declines per
tool. We deliberately don't install Docker, Python, or git — those need sudo,
GUI launches, or chicken-and-egg bootstrapping that doesn't belong in a
Python installer script.

Auto-installed tools land in user-local locations (no sudo):
  - gcloud:     ~/google-cloud-sdk/bin/gcloud
  - cloudflared: ~/.local/bin/cloudflared
  - uv:         ~/.local/bin/uv  (the astral.sh installer's default)

We prepend each install dir to `os.environ["PATH"]` for the rest of the
installer's run, so subsequent steps see the tool immediately. The operator
still has to `source ~/.zshrc` (or equivalent) for future shells — we print
the right line at the end.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import prompts

_HOME = Path.home()
_LOCAL_BIN = _HOME / ".local" / "bin"


def ensure_tool(
    name: str,
    *,
    auto_install: bool,
    install_fn,
) -> bool:
    """Return True if the tool is on PATH (after possible install)."""
    if shutil.which(name):
        prompts.ok(f"{name} found")
        return True

    prompts.warn(f"{name} not found on PATH")
    if not auto_install:
        prompts.info("  Install it yourself, then re-run the installer.")
        return False

    if not prompts.confirm(f"Auto-install {name} now? (no sudo required)", default=True):
        prompts.info(f"  Skipped. Install {name} manually and re-run.")
        return False

    install_fn()

    if not shutil.which(name):
        prompts.err(
            f"{name} install completed but {name} is still not on PATH. "
            "Open a new terminal (so it re-reads ~/.zshrc) and re-run the installer."
        )
        return False

    prompts.ok(f"{name} installed and on PATH")
    return True


# ---------------------------------------------------------------------------- #
# gcloud
# ---------------------------------------------------------------------------- #

def install_gcloud() -> None:
    """Install Google Cloud SDK to ~/google-cloud-sdk (no sudo).

    Downloads the platform-appropriate tarball, extracts, runs `install.sh
    --quiet` to add gcloud to PATH via the user's shell rc.
    """
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Darwin":
        if machine in ("arm64", "aarch64"):
            archive = "google-cloud-cli-darwin-arm.tar.gz"
        else:
            archive = "google-cloud-cli-darwin-x86_64.tar.gz"
    elif system == "Linux":
        archive = "google-cloud-cli-linux-x86_64.tar.gz"
    else:
        prompts.die(
            f"Auto-install of gcloud not supported on {system}. "
            "Install from https://cloud.google.com/sdk/docs/install"
        )

    url = f"https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/{archive}"
    install_dir = _HOME / "google-cloud-sdk"

    if install_dir.exists():
        prompts.info(f"  {install_dir} already exists; assuming a prior install")
    else:
        prompts.info(f"  Downloading {url}")
        with tempfile.TemporaryDirectory() as tmp:
            tarball = Path(tmp) / archive
            _download(url, tarball)
            prompts.info(f"  Extracting to {_HOME}")
            with tarfile.open(tarball) as tar:
                tar.extractall(_HOME)

    prompts.info("  Running install.sh (quiet mode, updates ~/.zshrc and ~/.bashrc)")
    subprocess.run(
        [
            str(install_dir / "install.sh"),
            "--quiet",
            "--usage-reporting", "false",
            "--command-completion", "true",
            "--path-update", "true",
        ],
        check=True,
    )

    # Make gcloud visible to the current process for the rest of this run.
    bin_dir = str(install_dir / "bin")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


# ---------------------------------------------------------------------------- #
# cloudflared
# ---------------------------------------------------------------------------- #

def install_cloudflared() -> None:
    """Download the cloudflared static binary to ~/.local/bin (no sudo)."""
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Darwin":
        if machine in ("arm64", "aarch64"):
            asset = "cloudflared-darwin-arm64.tgz"
            extracted_name = "cloudflared"
        else:
            asset = "cloudflared-darwin-amd64.tgz"
            extracted_name = "cloudflared"
    elif system == "Linux":
        asset = "cloudflared-linux-amd64"
        extracted_name = None  # direct binary, no extraction
    else:
        prompts.die(
            f"Auto-install of cloudflared not supported on {system}. "
            "Install from https://github.com/cloudflare/cloudflared/releases"
        )

    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
    _LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    target = _LOCAL_BIN / "cloudflared"

    with tempfile.TemporaryDirectory() as tmp:
        download_path = Path(tmp) / asset
        prompts.info(f"  Downloading {url}")
        _download(url, download_path)

        if extracted_name:
            # macOS ships a .tgz containing a single binary.
            with tarfile.open(download_path) as tar:
                tar.extractall(tmp)
            shutil.move(str(Path(tmp) / extracted_name), str(target))
        else:
            shutil.move(str(download_path), str(target))

    target.chmod(0o755)
    prompts.info(f"  Installed at {target}")

    _ensure_local_bin_on_path()


# ---------------------------------------------------------------------------- #
# uv
# ---------------------------------------------------------------------------- #

def install_uv() -> None:
    """Run the official astral.sh installer (non-interactive, no sudo).

    Installs to ~/.local/bin/uv and updates the user's shell rc files.
    """
    if platform.system() not in ("Darwin", "Linux"):
        prompts.die(
            f"Auto-install of uv not supported on {platform.system()}. "
            "See https://docs.astral.sh/uv/getting-started/installation/"
        )

    prompts.info("  Running: curl -LsSf https://astral.sh/uv/install.sh | sh")
    # We intentionally use a shell pipe here — astral's installer is signed by
    # Astral and serves over HTTPS; the URL is constant and audited. The
    # alternative (download + verify checksum + execute) would require
    # bundling the checksum, which goes stale.
    subprocess.run(
        "curl -LsSf https://astral.sh/uv/install.sh | sh",
        shell=True,
        check=True,
        env={**os.environ, "UV_NO_MODIFY_PATH": "0"},
    )
    _ensure_local_bin_on_path()


# ---------------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------------- #

def _download(url: str, dest: Path) -> None:
    """Stream a download with a coarse progress indicator."""
    req = urllib.request.Request(url, headers={"User-Agent": "gb-automations-installer/1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        chunk = 1024 * 64
        written = 0
        with open(dest, "wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                written += len(buf)
                if total and sys.stdout.isatty():
                    pct = (written / total) * 100
                    kb_now = written // 1024
                    kb_total = total // 1024
                    sys.stdout.write(f"\r  ↓ {kb_now} KB / {kb_total} KB ({pct:.0f}%)")
                    sys.stdout.flush()
    if sys.stdout.isatty():
        sys.stdout.write("\n")


def _ensure_local_bin_on_path() -> None:
    """Prepend ~/.local/bin to PATH for this process."""
    p = str(_LOCAL_BIN)
    if p not in os.environ["PATH"].split(os.pathsep):
        os.environ["PATH"] = f"{p}{os.pathsep}{os.environ['PATH']}"
