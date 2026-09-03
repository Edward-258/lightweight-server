"""Release orchestration: transaction, waves, rollback. Calls the generic bridge."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from cd.bridge import (
    activate,
    backup_runtime,
    checkout_units,
    full_refresh_units,
    restore_runtime,
)
from cd.catalog import (
    BUSINESS_COMPOSE_PATHS,
    HEALTHCHECK_ROLLBACK,
    UNITS,
    WAVES,
    affected_services,
    compose_runtime,
    units_named,
)
from cd.errors import CdError
from cd.linux import changed_paths
from cd_state import (
    current_identity,
    deploy_lock,
    read_baseline,
    read_state,
    write_state,
)


def encode_touched_waves(waves: list[tuple[str, tuple[str, ...]]]) -> str:
    return json.dumps(
        [{"name": name, "services": list(services)} for name, services in waves],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_touched_waves(value: object) -> list[tuple[str, tuple[str, ...]]]:
    if not value:
        return []
    if isinstance(value, list):
        entries = value
    elif isinstance(value, str):
        try:
            entries = json.loads(value)
        except json.JSONDecodeError:
            return []
    else:
        return []
    if not isinstance(entries, list):
        return []
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        services = entry.get("services")
        if isinstance(name, str) and isinstance(services, list):
            result.append((name, tuple(str(service) for service in services)))
    return result


def rollback_order(waves: list[tuple[str, tuple[str, ...]]]) -> list[tuple[str, tuple[str, ...]]]:
    """Return touched waves in reverse dependency order without duplicates."""
    seen: set[str] = set()
    ordered = []
    for wave in reversed(waves):
        if wave[0] not in seen:
            ordered.append(wave)
            seen.add(wave[0])
    return ordered


def release_context() -> tuple[dict, str, str, str]:
    try:
        state = read_state()
        baseline = read_baseline()
        identity = current_identity()
    except (OSError, ValueError) as exc:
        raise CdError(f"cd-compose: 外置发布状态无效: {exc}", 2) from exc
    release_sha = str(state.get("release_sha") or "").lower()
    baseline_sha = str(state.get("baseline_sha") or "").lower()
    if not release_sha or not baseline_sha:
        raise CdError("cd-compose: 发布事务缺少 release_sha 或 baseline_sha", 2)
    if identity != str(state.get("identity") or ""):
        raise CdError("cd-compose: 发布身份不匹配，拒绝继续", 2)
    if baseline["success_sha"] != baseline_sha:
        raise CdError("cd-compose: 成功基线已变化，拒绝继续旧事务", 2)
    expected_sha = os.environ.get("CI_COMMIT_SHA", "").strip().lower()
    if expected_sha != release_sha:
        raise CdError("cd-compose: CI_COMMIT_SHA 与发布事务不匹配", 2)
    return state, release_sha, baseline_sha, identity


def assert_transaction_state(release_sha: str, baseline_sha: str) -> None:
    current = read_state()
    if current.get("release_sha") != release_sha or current.get("baseline_sha") != baseline_sha:
        raise CdError("cd-compose: 发布事务已被其他尝试覆盖，拒绝继续", 2)


def select_waves(
    only_units: tuple[str, ...] | None,
    only_waves: tuple[str, ...] | None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if only_units:
        unknown = [name for name in only_units if name not in UNITS]
        if unknown:
            known = ", ".join(UNITS)
            raise CdError(f"cd-compose: 未知节点 {', '.join(unknown)}（{known}）")
    if only_waves:
        known_waves = {name for name, _ in WAVES}
        unknown = [name for name in only_waves if name not in known_waves]
        if unknown:
            raise CdError(f"cd-compose: 未知波次 {', '.join(unknown)}")
    wanted_units = set(only_units) if only_units else None
    wanted_waves = set(only_waves) if only_waves else None
    selected: list[tuple[str, tuple[str, ...]]] = []
    for wave, names in WAVES:
        if wanted_waves is not None and wave not in wanted_waves:
            continue
        picked = names if wanted_units is None else tuple(n for n in names if n in wanted_units)
        if picked:
            selected.append((wave, picked))
    return tuple(selected)


def _resolve_shas(dry_run: bool) -> tuple[dict, str, str]:
    if dry_run:
        state = {
            "release_sha": "a" * 40,
            "baseline_sha": "b" * 40,
            "identity": "pipeline:dry-run:sha:" + "a" * 40,
        }
        return state, state["release_sha"], state["baseline_sha"]
    state, new_sha, prev_sha, _ = release_context()
    return state, new_sha, prev_sha


def rollback_waves(
    root: Path,
    waves: list[tuple[str, tuple[str, ...]]],
    prev: str,
    dry_run: bool,
) -> None:
    """Restore all touched waves, then reactivate them in reverse DAG order."""
    if not waves:
        return
    names = tuple(dict.fromkeys(service for _, group in waves for service in group if service in UNITS))
    units = units_named(names)
    runtime = compose_runtime(root)
    for unit in units:
        restore_runtime(root, unit, dry_run)
    checkout_units(runtime, units, prev, dry_run, extra_paths=BUSINESS_COMPOSE_PATHS)
    failures: list[str] = []
    for wave, wave_names in rollback_order(waves):
        print(f"cd-compose: ROLLBACK wave={wave} services={','.join(wave_names)} -> {prev}")
        try:
            full_refresh_units(runtime, units_named(tuple(n for n in wave_names if n in UNITS)), dry_run)
        except CdError as exc:
            failures.append(f"{wave}: {exc}")
            print(
                f"cd-compose: 回滚 wave={wave} 时 compose 仍失败: {exc}",
                file=sys.stderr,
            )
    if failures:
        raise CdError("cd-compose: 级联回滚失败: " + "; ".join(failures))


def _deploy(
    root: Path,
    dry_run: bool,
    only_units: tuple[str, ...] | None = None,
    only_waves: tuple[str, ...] | None = None,
) -> None:
    if not dry_run and not (root / "docker-compose.yml").is_file():
        raise CdError(f"cd-compose: 找不到 {root}/docker-compose.yml", 2)
    state, new_sha, prev_sha = _resolve_shas(dry_run)
    planned_waves = select_waves(only_units, only_waves)
    paths = (
        ("<initial-release>",)
        if dry_run
        else changed_paths(root, prev_sha, new_sha)
    )
    affected = set(affected_services(paths))
    full_refresh = "docker-compose.yml" in paths or "<initial-release>" in paths
    touched: list[tuple[str, tuple[str, ...]]] = []
    runtime = compose_runtime(root)
    print(f"cd-compose: deployment waves new={new_sha} prev={prev_sha} changed={paths}")
    if not dry_run:
        write_state(
            {
                **state,
                "phase": "deploying",
                "touched_waves": encode_touched_waves(touched),
            },
        )
    for wave, wave_names in planned_waves:
        names = tuple(name for name in wave_names if name in affected)
        if not names:
            print(f"cd-compose: wave={wave} 无相关变更，跳过刷新")
            continue
        touched.append((wave, names))
        if not dry_run:
            write_state(
                {
                    **read_state(),
                    "phase": "deploying",
                    "touched_waves": encode_touched_waves(touched),
                },
            )
        print(f"cd-compose: wave={wave} services={','.join(names)}")
        wave_units = units_named(names)
        for unit in wave_units:
            backup_runtime(root, unit, dry_run)
        try:
            if not dry_run:
                assert_transaction_state(new_sha, prev_sha)
            checkout_units(runtime, wave_units, new_sha, dry_run)
            activate(runtime, wave_units, paths, dry_run, full_refresh=full_refresh)
        except CdError:
            if prev_sha:
                try:
                    rollback_waves(root, touched, prev_sha, dry_run)
                except CdError:
                    if not dry_run:
                        write_state(
                            {
                                **read_state(),
                                "phase": "rollback_failed",
                                "touched_waves": encode_touched_waves(touched),
                            },
                        )
                    raise
                if not dry_run:
                    write_state(
                        {
                            **read_state(),
                            "phase": "rolled_back",
                            "touched_waves": encode_touched_waves(touched),
                        },
                    )
            raise
        print(f"cd-compose: wave={wave} 完成")


def deploy(
    root: Path,
    dry_run: bool,
    only_units: tuple[str, ...] | None = None,
    only_waves: tuple[str, ...] | None = None,
) -> None:
    """Serialize the complete (or filtered) business deployment on the shared host."""
    with deploy_lock(root):
        _deploy(root, dry_run, only_units, only_waves)


def _rollback_healthcheck(root: Path, dry_run: bool) -> int:
    if dry_run:
        state = {
            "release_sha": "a" * 40,
            "baseline_sha": "b" * 40,
            "identity": "pipeline:dry-run:sha:" + "a" * 40,
        }
        prev_sha = state["baseline_sha"]
    else:
        state, _, prev_sha, _ = release_context()
    if not prev_sha:
        print("cd-compose: healthcheck 失败但没有 prev_sha，无法回滚", file=sys.stderr)
        return 1
    touched = decode_touched_waves(state.get("touched_waves", ""))
    if not touched:
        touched = [(name, (name,)) for name in HEALTHCHECK_ROLLBACK]
    print("cd-compose: healthcheck 失败，按已触碰波次逆序回滚")
    rollback_waves(root, touched, prev_sha, dry_run)
    if not dry_run:
        write_state(
            {
                **read_state(),
                "phase": "rolled_back",
                "touched_waves": encode_touched_waves(touched),
            },
        )
    return 1


def rollback_healthcheck(root: Path, dry_run: bool) -> int:
    with deploy_lock(root):
        return _rollback_healthcheck(root, dry_run)


def mark_healthcheck(root: Path, dry_run: bool) -> None:
    if dry_run:
        print("cd-compose: dry-run，标记 healthcheck_passed")
        return
    with deploy_lock(root):
        state, _, _, _ = release_context()
        if state.get("phase") != "deploying":
            raise CdError("cd-compose: 事务不在 deploying 阶段，拒绝标记健康检查")
        write_state({**state, "phase": "healthcheck_passed"})
    print("cd-compose: healthcheck_passed")
