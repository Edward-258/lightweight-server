"""Docker / compose primitives. No service catalog, no wave DAG."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from cd.errors import CdError


@dataclass(frozen=True)
class ComposeRuntime:
    """How to talk to one compose project on one host tree."""

    root: Path
    project: str
    compose_file: str
    forbidden: frozenset[str] = field(default_factory=frozenset)

    def compose_cmd(self, *args: str) -> list[str]:
        return [
            "docker",
            "compose",
            "-p",
            self.project,
            "--project-directory",
            str(self.root),
            "-f",
            str(self.root / self.compose_file),
            *args,
        ]


def run(cmd: list[str], dry_run: bool) -> None:
    print("+", " ".join(cmd))
    if dry_run:
        return
    try:
        p = subprocess.run(cmd)
    except FileNotFoundError as exc:
        raise CdError(
            "cd-compose: 未找到 docker。宿主机先: docker compose up -d --build promtail-sd",
            2,
        ) from exc
    if p.returncode != 0:
        raise CdError(f"cd-compose: 命令失败 exit {p.returncode}", p.returncode or 1)


def compose(runtime: ComposeRuntime, *args: str, dry_run: bool) -> None:
    run(runtime.compose_cmd(*args), dry_run)


def docker_exec(container: str, argv: tuple[str, ...], dry_run: bool) -> None:
    run(["docker", "exec", container, *argv], dry_run)


def docker_kill(container: str, signal: str, dry_run: bool) -> None:
    run(["docker", "kill", f"--signal={signal}", container], dry_run)
