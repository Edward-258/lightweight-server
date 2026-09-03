"""Generic compose bridge.

Callers pass Unit specs. This module does not know nginx/gitea/grafana by name.
Reload vs recreate vs oneshot is data on the unit, executed via docker/linux primitives.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cd.docker import ComposeRuntime, compose, docker_exec, docker_kill
from cd.errors import CdError
from cd.linux import checkout, path_matches, unique_paths
from cd_state import state_backup_path


@dataclass(frozen=True)
class Unit:
    name: str
    paths: tuple[str, ...]
    impact_paths: tuple[str, ...]
    services: tuple[str, ...] = ()
    recreate_paths: tuple[str, ...] = ()
    reload_paths: tuple[str, ...] = ()
    build_paths: tuple[str, ...] = ()
    runtime: tuple[str, ...] = ()
    rollback_services: tuple[str, ...] = ()
    oneshot: bool = False
    container: str = ""
    reload_exec: tuple[tuple[str, ...], ...] = ()
    reload_signal: str | None = None

    def compose_services(self) -> tuple[str, ...]:
        return self.services or (self.name,)

    def exec_container(self) -> str:
        return self.container or self.name

    def rollback_names(self) -> tuple[str, ...]:
        return self.rollback_services or self.compose_services()


def action_for(unit: Unit, paths: tuple[str, ...], full_refresh: bool = False) -> str:
    if unit.oneshot:
        return "oneshot"
    if any(path_matches(path, unit.build_paths) for path in paths) or (
        full_refresh and unit.build_paths
    ):
        return "build+recreate"
    if full_refresh:
        return "recreate"
    if any(path_matches(path, unit.recreate_paths) for path in paths):
        return "recreate"
    if any(path_matches(path, unit.reload_paths) for path in paths):
        return "reload"
    return "recreate"


def actions_for(
    units: Sequence[Unit], paths: tuple[str, ...], full_refresh: bool = False
) -> dict[str, str]:
    return {unit.name: action_for(unit, paths, full_refresh) for unit in units}


def unit_is_affected(unit: Unit, paths: tuple[str, ...]) -> bool:
    patterns = unit.impact_paths or unit.paths
    return any(path_matches(path, patterns) for path in paths)


def refuse_forbidden(runtime: ComposeRuntime, names: tuple[str, ...]) -> None:
    bad = [name for name in names if name in runtime.forbidden]
    if bad:
        raise CdError(f"cd-compose: 拒绝操作控制面: {', '.join(bad)}")


def backup_runtime(root: Path, unit: Unit, dry_run: bool) -> None:
    dest = state_backup_path() / unit.name
    if dry_run:
        print(f"cd-compose: backup runtime {unit.runtime} -> {dest}")
        return
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for rel in unit.runtime:
        src = root / rel
        if src.is_file():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


def restore_runtime(root: Path, unit: Unit, dry_run: bool) -> None:
    dest = state_backup_path() / unit.name
    for rel in unit.runtime:
        backup = dest / rel
        src = root / rel
        print(f"cd-compose: restore runtime {rel}")
        if dry_run:
            continue
        if backup.is_file():
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, src)


def reload_unit(unit: Unit, dry_run: bool) -> None:
    if not unit.reload_exec and not unit.reload_signal:
        raise CdError(f"cd-compose: {unit.name} 不支持 reload")
    container = unit.exec_container()
    for argv in unit.reload_exec:
        docker_exec(container, argv, dry_run)
    if unit.reload_signal:
        docker_kill(container, unit.reload_signal, dry_run)


def activate(
    runtime: ComposeRuntime,
    units: Sequence[Unit],
    paths: tuple[str, ...],
    dry_run: bool,
    *,
    full_refresh: bool = False,
) -> None:
    """Apply the smallest safe refresh action for a set of units."""
    names = tuple(name for unit in units for name in unit.compose_services())
    refuse_forbidden(runtime, names)
    planned = actions_for(units, paths, full_refresh)
    print(
        "cd-compose: actions "
        + ", ".join(f"{name}={action}" for name, action in planned.items())
    )
    by_name = {unit.name: unit for unit in units}

    for name, action in planned.items():
        if action == "build+recreate":
            compose(runtime, "build", "--pull=never", name, dry_run=dry_run)

    for name, action in planned.items():
        if action == "oneshot":
            compose(
                runtime,
                "run",
                "--rm",
                "--no-deps",
                "--pull=never",
                name,
                dry_run=dry_run,
            )

    for name, action in planned.items():
        if action == "reload":
            reload_unit(by_name[name], dry_run)

    recreates = tuple(
        name
        for name, action in planned.items()
        if action in {"recreate", "build+recreate"} and not by_name[name].oneshot
    )
    if recreates:
        refuse_forbidden(runtime, recreates)
        compose(
            runtime,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--pull=never",
            *recreates,
            dry_run=dry_run,
        )


def recreate_units(runtime: ComposeRuntime, units: Sequence[Unit], dry_run: bool) -> None:
    """Rollback helper: restore units with force-recreate, never reload only."""
    names = tuple(name for unit in units for name in unit.compose_services())
    refuse_forbidden(runtime, names)
    for unit in units:
        if unit.oneshot:
            compose(
                runtime,
                "run",
                "--rm",
                "--no-deps",
                "--pull=never",
                unit.name,
                dry_run=dry_run,
            )
    regular = tuple(
        name
        for unit in units
        for name in unit.compose_services()
        if not unit.oneshot
    )
    if not regular:
        return
    compose(
        runtime,
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--pull=never",
        *regular,
        dry_run=dry_run,
    )


def full_refresh_units(runtime: ComposeRuntime, units: Sequence[Unit], dry_run: bool) -> None:
    activate(runtime, units, ("<initial-release>",), dry_run, full_refresh=True)


def checkout_units(
    runtime: ComposeRuntime,
    units: Sequence[Unit],
    sha: str,
    dry_run: bool,
    extra_paths: tuple[str, ...] = (),
) -> None:
    paths = unique_paths(extra_paths, *(unit.paths for unit in units))
    checkout(runtime.root, sha, paths, dry_run)
