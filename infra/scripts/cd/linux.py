"""Linux/git primitives. No compose knowledge, no service names."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from cd.errors import CdError
from cd_state import git_lock


def git(root: Path, args: list[str]) -> str:
    cmd = ["git", "-c", f"safe.directory={root}", "-C", str(root), *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise CdError("cd-compose: 未找到 git", 2) from exc
    if p.returncode != 0:
        err = (p.stderr or p.stdout or f"exit {p.returncode}").strip()
        raise CdError(f"cd-compose: git {' '.join(args)} 失败: {err}", 2)
    return p.stdout


def unique_paths(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(path for group in groups for path in group))


def path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(path == pattern or path.startswith(pattern) for pattern in patterns)


def changed_paths(root: Path, previous: str, current: str) -> tuple[str, ...]:
    if not previous:
        return ("<initial-release>",)
    if previous == current:
        return ()
    output = git(root, ["diff", "--name-only", previous, current])
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def checkout(root: Path, sha: str, paths: tuple[str, ...], dry_run: bool) -> None:
    if not sha:
        raise CdError("cd-compose: 缺少 SHA，无法 checkout", 2)
    if not paths:
        return
    cmd = ["git", "-c", f"safe.directory={root}", "-C", str(root), "checkout", sha, "--", *paths]
    print("+", " ".join(cmd))
    if dry_run:
        return
    with git_lock(root):
        existing = []
        missing = []
        for path in paths:
            if git(root, ["ls-tree", "-r", "--name-only", sha, "--", path]).strip():
                existing.append(path)
            else:
                missing.append(path)
        if existing:
            git(root, ["checkout", sha, "--", *existing])
        for path in missing:
            target = root / path.rstrip("/")
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
