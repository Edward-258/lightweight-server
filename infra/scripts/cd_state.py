"""External CD baseline, transaction state, and host-wide locking."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import re
import tempfile
from typing import Any

try:
    import fcntl
except ImportError:  # Windows local dry-run
    fcntl = None  # type: ignore[assignment]

STATE_ROOT = "/var/lib/infra-cd"
BASELINE_NAME = "baseline.json"
TRANSACTION_NAME = "transaction.json"
DEPLOY_LOCK_NAME = "deploy.lock"
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def state_root() -> Path:
    return Path(os.environ.get("CD_STATE_ROOT", STATE_ROOT))


def ensure_state_root() -> Path:
    path = state_root()
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def baseline_path() -> Path:
    return state_root() / BASELINE_NAME


def transaction_path() -> Path:
    return state_root() / TRANSACTION_NAME


def deploy_lock_path() -> Path:
    return state_root() / DEPLOY_LOCK_NAME


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def read_baseline() -> dict[str, str]:
    data = read_json(baseline_path())
    sha = data.get("success_sha")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise ValueError(f"invalid success_sha in {baseline_path()}")
    return {"success_sha": sha.lower()}


def read_state() -> dict[str, Any]:
    return read_json(transaction_path())


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(temp_name)
    try:
        payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        try:
            os.fchmod(fd, 0o600)
        except AttributeError:  # Windows local tests; ECS uses fchmod.
            os.chmod(temp, 0o600)
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
        os.close(fd)
        os.replace(temp, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except (AttributeError, PermissionError, OSError) as exc:
            if os.name == "nt":
                return
            raise exc
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        raise


def write_baseline(sha: str) -> None:
    if not SHA_RE.fullmatch(sha):
        raise ValueError(f"invalid baseline SHA: {sha}")
    write_json_atomic(
        baseline_path(),
        {
            "version": 1,
            "success_sha": sha.lower(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def write_state(data: dict[str, Any]) -> None:
    write_json_atomic(transaction_path(), data)


def state_backup_path() -> Path:
    return state_root() / "backups"


def current_identity() -> str:
    pipeline = (
        os.environ.get("CI_PIPELINE_NUMBER")
        or os.environ.get("CI_PIPELINE_ID")
        or os.environ.get("CI_BUILD_NUMBER")
        or ""
    ).strip()
    sha = os.environ.get("CI_COMMIT_SHA", "").strip()
    if not pipeline or not SHA_RE.fullmatch(sha):
        raise ValueError("CI_PIPELINE_NUMBER/CI_PIPELINE_ID and CI_COMMIT_SHA are required")
    return f"pipeline:{pipeline}:sha:{sha.lower()}"


def validate_sha(sha: str, label: str = "SHA") -> str:
    value = sha.strip().lower()
    if not SHA_RE.fullmatch(value):
        raise ValueError(f"invalid {label}: {sha}")
    return value


@contextmanager
def git_lock(root: Path) -> Iterator[None]:
    """Exclusive lock for Git operations on the shared working tree."""
    if fcntl is None:
        yield
        return
    path = root / ".cd-git.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def deploy_lock(root: Path) -> Iterator[None]:
    """Serialize each release operation on one host."""
    if fcntl is None:
        yield
        return
    path = deploy_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
