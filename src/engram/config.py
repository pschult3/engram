"""Runtime configuration for engram.

The store lives under ENGRAM_HOME (default: ~/.engram) with one SQLite
database per project namespace. The project namespace defaults to the git
toplevel basename plus a short path hash, so two repos named `api/` in
different locations never collide. Override via ENGRAM_PROJECT.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p))).resolve()


def _git_toplevel(cwd: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return Path(out) if out else None


def _project_key(cwd: Path) -> str:
    """Stable project key.

    Uses git toplevel basename + short hash of the absolute path. This means:
      - two repos called `api` in different locations get distinct stores
      - moving a repo creates a new store (intentional — the move could
        change context)
      - non-git directories fall back to cwd basename + path hash
    """
    base = _git_toplevel(cwd) or cwd
    name = base.name or "default"
    h = hashlib.sha1(str(base).encode("utf-8")).hexdigest()[:8]
    return f"{name}-{h}"


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


@dataclass(frozen=True)
class Config:
    home: Path
    project: str
    log_level: str

    @property
    def project_dir(self) -> Path:
        return self.home / "projects" / self.project

    @property
    def db_path(self) -> Path:
        return self.project_dir / "memory.db"

    @property
    def queue_path(self) -> Path:
        return self.project_dir / "events.ndjson"

    def ensure_dirs(self) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)


def load_config(cwd: Path | None = None) -> Config:
    home = _expand(os.environ.get("ENGRAM_HOME", "~/.engram"))
    cwd = cwd or Path.cwd()
    project = os.environ.get("ENGRAM_PROJECT", "").strip() or _project_key(cwd)
    project = _sanitize(project)
    log_level = os.environ.get("ENGRAM_LOG_LEVEL", "INFO")
    cfg = Config(home=home, project=project, log_level=log_level)
    cfg.ensure_dirs()
    return cfg
