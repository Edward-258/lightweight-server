#!/usr/bin/env python3
"""CD: path-scoped compose up per deployment wave. Never deploy woodpecker-server/agent.

On failure, restore this node's files from prev_sha and compose up again.
healthcheck 失败时回滚 nginx + grafana。

The DAG owns service ordering. am-config is an explicit one-shot render step;
service starts must not traverse Compose dependencies and run it again.

Woodpecker 在 docker-compose.woodpecker.yml（项目 woodpecker），本脚本只操作
-p infra -f docker-compose.yml，因此不会重建控制面，也不 drain agent。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cd_state import (
    current_identity,
    deploy_lock,
    git_lock,
    read_baseline,
    read_state,
    state_backup_path,
    write_state,
)

DEFAULT_ROOT = "/root/infra"
FORBIDDEN = frozenset({"woodpecker-server", "woodpecker-agent"})
COMPOSE_PROJECT = "infra"
BUSINESS_COMPOSE_PATHS = ("docker-compose.yml", "compose/")

# services 默认等于节点名；paths/runtime/refresh paths 按需覆盖。
NODES: dict[str, dict] = {
    "gitea": {
        "paths": ("config/", "compose/core.yml"),
        "impact_paths": ("compose/core.yml",),
        "recreate_paths": ("compose/core.yml",),
    },
    "am-config": {
        "paths": ("alertmanager/", "compose/alerting.yml"),
        "impact_paths": ("alertmanager/", "compose/alerting.yml"),
        "runtime": ("alertmanager/alertmanager.runtime.yml",),
        "oneshot": True,
    },
    "node_exporter": {
        "paths": ("compose/metrics.yml",),
        "impact_paths": ("compose/metrics.yml",),
        "recreate_paths": ("compose/metrics.yml",),
    },
    "loki": {
        "paths": ("loki/", "compose/logging.yml"),
        "impact_paths": ("loki/", "compose/logging.yml"),
        "recreate_paths": ("loki/", "compose/logging.yml"),
    },
    "promtail-sd": {
        "paths": ("python/", "promtail/sd.py", "compose/logging.yml"),
        "impact_paths": ("python/", "promtail/sd.py", "compose/logging.yml"),
        "recreate_paths": ("promtail/sd.py", "compose/logging.yml"),
        "build_paths": ("python/",),
    },
    "alertmanager": {
        "paths": ("alertmanager/", "compose/alerting.yml"),
        "impact_paths": ("alertmanager/", "compose/alerting.yml"),
        "runtime": ("alertmanager/alertmanager.runtime.yml",),
        "rollback_services": ("am-config", "alertmanager"),
        "reload_paths": ("alertmanager/",),
        "recreate_paths": ("compose/alerting.yml",),
    },
    "promtail": {
        "paths": ("promtail/", "compose/logging.yml"),
        "impact_paths": ("promtail/", "compose/logging.yml"),
        "recreate_paths": ("promtail/", "compose/logging.yml"),
    },
    "prometheus": {
        "paths": ("prometheus/", "compose/metrics.yml"),
        "impact_paths": ("prometheus/", "compose/metrics.yml"),
        "reload_paths": ("prometheus/",),
        "recreate_paths": ("compose/metrics.yml",),
    },
    "grafana": {
        "paths": ("grafana/", "compose/grafana.yml"),
        "impact_paths": ("grafana/", "compose/grafana.yml"),
        "recreate_paths": ("grafana/", "compose/grafana.yml"),
    },
    "nginx": {
        "paths": ("nginx/", "compose/core.yml"),
        "impact_paths": ("nginx/", "compose/core.yml"),
        "reload_paths": ("nginx/",),
        "recreate_paths": ("compose/core.yml",),
    },
}

HEALTHCHECK_ROLLBACK = ("nginx", "grafana")
WAVES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("core", ("gitea",)),
    ("core-proxy", ("nginx",)),
    ("alerting", ("am-config", "alertmanager")),
    ("foundation", ("node_exporter", "loki", "promtail-sd")),
    ("dependent", ("prometheus", "promtail")),
    ("grafana", ("grafana",)),
)


def node_services(name: str) -> tuple[str, ...]:
    spec = NODES[name]
    return tuple(spec.get("services") or (name,))


def node_paths(name: str) -> tuple[str, ...]:
    return tuple(NODES[name].get("paths") or ())


def node_runtime(name: str) -> tuple[str, ...]:
    return tuple(NODES[name].get("runtime") or ())


def backup_root(root: Path) -> Path:
    """Keep rollback data on the shared host tree across Woodpecker steps."""
    del root
    return state_backup_path()


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


def affected_services(paths: tuple[str, ...]) -> tuple[str, ...]:
    if not paths:
        return ()
    if "<initial-release>" in paths or "docker-compose.yml" in paths:
        return tuple(name for _, names in WAVES for name in names if name in NODES)
    names: list[str] = []
    for _, wave_names in WAVES:
        for name in wave_names:
            patterns = tuple(NODES[name].get("impact_paths") or node_paths(name))
            if any(path_matches(path, patterns) for path in paths):
                names.append(name)
    return tuple(names)


def service_action(name: str, paths: tuple[str, ...], full_refresh: bool = False) -> str:
    spec = NODES[name]
    if spec.get("oneshot"):
        return "oneshot"
    build = any(path_matches(path, tuple(spec.get("build_paths") or ())) for path in paths)
    if build or (full_refresh and spec.get("build_paths")):
        return "build+recreate"
    if full_refresh:
        return "recreate"
    if any(path_matches(path, tuple(spec.get("recreate_paths") or ())) for path in paths):
        return "recreate"
    if any(path_matches(path, tuple(spec.get("reload_paths") or ())) for path in paths):
        return "reload"
    return "recreate"


def service_actions(
    names: tuple[str, ...], paths: tuple[str, ...], full_refresh: bool = False
) -> dict[str, str]:
    return {name: service_action(name, paths, full_refresh) for name in names}


def encode_touched_waves(waves: list[tuple[str, tuple[str, ...]]]) -> str:
    return json.dumps(
        [{"name": name, "services": list(services)} for name, services in waves],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_touched_waves(value: str) -> list[tuple[str, tuple[str, ...]]]:
    if not value:
        return []
    try:
        entries = json.loads(value)
    except json.JSONDecodeError:
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


def release_context() -> tuple[dict, str, str, str]:
    try:
        state = read_state()
        baseline = read_baseline()
        identity = current_identity()
    except (OSError, ValueError) as exc:
        raise ComposeError(f"cd-compose: 外置发布状态无效: {exc}", 2) from exc
    release_sha = str(state.get("release_sha") or "").lower()
    baseline_sha = str(state.get("baseline_sha") or "").lower()
    if not release_sha or not baseline_sha:
        raise ComposeError("cd-compose: 发布事务缺少 release_sha 或 baseline_sha", 2)
    if identity != str(state.get("identity") or ""):
        raise ComposeError("cd-compose: 发布身份不匹配，拒绝继续", 2)
    if baseline["success_sha"] != baseline_sha:
        raise ComposeError("cd-compose: 成功基线已变化，拒绝继续旧事务", 2)
    expected_sha = os.environ.get("CI_COMMIT_SHA", "").strip().lower()
    if expected_sha != release_sha:
        raise ComposeError("cd-compose: CI_COMMIT_SHA 与发布事务不匹配", 2)
    return state, release_sha, baseline_sha, identity


def assert_transaction_state(state: dict, release_sha: str, baseline_sha: str) -> None:
    current = read_state()
    if current.get("release_sha") != release_sha or current.get("baseline_sha") != baseline_sha:
        raise ComposeError("cd-compose: 发布事务已被其他尝试覆盖，拒绝继续", 2)


def rollback_order(waves: list[tuple[str, tuple[str, ...]]]) -> list[tuple[str, tuple[str, ...]]]:
    """Return touched waves in reverse dependency order without duplicates."""
    seen: set[str] = set()
    ordered = []
    for wave in reversed(waves):
        if wave[0] not in seen:
            ordered.append(wave)
            seen.add(wave[0])
    return ordered


class ComposeError(Exception):
    def __init__(self, msg: str, code: int = 1) -> None:
        super().__init__(msg)
        self.code = code


def git(root: Path, args: list[str]) -> str:
    cmd = ["git", "-c", f"safe.directory={root}", "-C", str(root), *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ComposeError("cd-compose: 未找到 git", 2) from exc
    if p.returncode != 0:
        err = (p.stderr or p.stdout or f"exit {p.returncode}").strip()
        raise ComposeError(f"cd-compose: git {' '.join(args)} 失败: {err}", 2)
    return p.stdout


def compose_cmd(root: Path, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        COMPOSE_PROJECT,
        "--project-directory",
        str(root),
        "-f",
        str(root / "docker-compose.yml"),
        *args,
    ]


def run(cmd: list[str], dry_run: bool) -> None:
    print("+", " ".join(cmd))
    if dry_run:
        return
    try:
        p = subprocess.run(cmd)
    except FileNotFoundError as exc:
        raise ComposeError(
            "cd-compose: 未找到 docker。宿主机先: docker compose up -d --build promtail-sd",
            2,
        ) from exc
    if p.returncode != 0:
        raise ComposeError(f"cd-compose: 命令失败 exit {p.returncode}", p.returncode or 1)


def refuse_forbidden(names: tuple[str, ...]) -> None:
    bad = [name for name in names if name in FORBIDDEN]
    if bad:
        raise ComposeError(f"cd-compose: 拒绝操作控制面: {', '.join(bad)}")


def checkout(root: Path, sha: str, paths: tuple[str, ...], dry_run: bool) -> None:
    if not sha:
        raise ComposeError("cd-compose: 缺少 SHA，无法 checkout", 2)
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


def backup_runtime(root: Path, group: str, dry_run: bool) -> None:
    files = node_runtime(group)
    dest = backup_root(root) / group
    if dry_run:
        print(f"cd-compose: backup runtime {files} -> {dest}")
        return
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for rel in files:
        src = root / rel
        if src.is_file():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)


def restore_runtime(root: Path, group: str, dry_run: bool) -> None:
    dest = backup_root(root) / group
    for rel in node_runtime(group):
        backup = dest / rel
        src = root / rel
        print(f"cd-compose: restore runtime {rel}")
        if dry_run:
            continue
        if backup.is_file():
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, src)


def reload_service(root: Path, name: str, dry_run: bool) -> None:
    if name == "nginx":
        run(["docker", "exec", "nginx-proxy", "nginx", "-t"], dry_run)
        run(["docker", "exec", "nginx-proxy", "nginx", "-s", "reload"], dry_run)
    elif name == "prometheus":
        run(
            [
                "docker",
                "exec",
                "prometheus",
                "promtool",
                "check",
                "config",
                "/etc/prometheus/prometheus.yml",
            ],
            dry_run,
        )
        run(
            [
                "docker",
                "exec",
                "prometheus",
                "promtool",
                "check",
                "rules",
                "/etc/prometheus/rules.yml",
            ],
            dry_run,
        )
        run(
            [
                "docker",
                "exec",
                "prometheus",
                "wget",
                "-q",
                "--post-data=",
                "http://127.0.0.1:9090/-/reload",
                "-O",
                "/dev/null",
            ],
            dry_run,
        )
    elif name == "alertmanager":
        run(
            [
                "docker",
                "exec",
                "alertmanager",
                "amtool",
                "check-config",
                "/etc/alertmanager/alertmanager.yml",
            ],
            dry_run,
        )
        run(["docker", "kill", "--signal=HUP", "alertmanager"], dry_run)
    else:
        raise ComposeError(f"cd-compose: {name} 不支持 reload")


def build_service(root: Path, name: str, dry_run: bool) -> None:
    run(compose_cmd(root, "build", "--pull=never", name), dry_run)


def recreate_services(root: Path, names: tuple[str, ...], dry_run: bool) -> None:
    refuse_forbidden(names)
    regular = tuple(name for name in names if not NODES.get(name, {}).get("oneshot"))
    if not regular:
        return
    args = ["up", "-d", "--no-deps", "--force-recreate", "--pull=never", *regular]
    run(compose_cmd(root, *args), dry_run)


def wave_paths(names: tuple[str, ...]) -> tuple[str, ...]:
    return unique_paths(*(node_paths(name) for name in names))


def activate_services(
    root: Path,
    names: tuple[str, ...],
    paths: tuple[str, ...],
    dry_run: bool,
    *,
    full_refresh: bool = False,
) -> None:
    """Apply the smallest safe refresh action for one deployment wave."""
    refuse_forbidden(names)
    actions = service_actions(names, paths, full_refresh)
    print(
        "cd-compose: actions "
        + ", ".join(f"{name}={action}" for name, action in actions.items())
    )
    rebuild = tuple(name for name, action in actions.items() if action == "build+recreate")
    for name in rebuild:
        build_service(root, name, dry_run)

    oneshot = tuple(name for name, action in actions.items() if action == "oneshot")
    for name in oneshot:
        run(
            compose_cmd(
                root,
                "run",
                "--rm",
                "--no-deps",
                "--pull=never",
                name,
            ),
            dry_run,
        )

    reloads = tuple(name for name, action in actions.items() if action == "reload")
    for name in reloads:
        reload_service(root, name, dry_run)

    recreates = tuple(
        name
        for name, action in actions.items()
        if action in {"recreate", "build+recreate"}
        and not NODES.get(name, {}).get("oneshot")
    )
    if recreates:
        recreate_services(root, recreates, dry_run)


def up_wave_services(root: Path, names: tuple[str, ...], dry_run: bool) -> None:
    """Compatibility helper for callers that need a full wave refresh."""
    activate_services(root, names, ("<initial-release>",), dry_run, full_refresh=True)


def up_services(root: Path, names: tuple[str, ...], dry_run: bool) -> None:
    """Rollback helper: restore services with force-recreate, never reload only."""
    refuse_forbidden(names)
    for name in names:
        if NODES.get(name, {}).get("oneshot"):
            run(
                compose_cmd(root, "run", "--rm", "--no-deps", "--pull=never", name),
                dry_run,
            )
    recreate_services(root, names, dry_run)


def rollback_group(root: Path, group: str, prev: str, dry_run: bool) -> None:
    print(f"cd-compose: ROLLBACK {group} -> {prev}")
    spec = NODES[group]
    services = tuple(spec.get("rollback_services") or node_services(group))
    refuse_forbidden(services)
    restore_runtime(root, group, dry_run)
    checkout(root, prev, (*BUSINESS_COMPOSE_PATHS, *node_paths(group)), dry_run)
    try:
        up_services(root, services, dry_run)
    except ComposeError as exc:
        print(f"cd-compose: 回滚 {group} 时 compose 仍失败: {exc}", file=sys.stderr)


def rollback_wave(root: Path, names: tuple[str, ...], prev: str, dry_run: bool) -> None:
    rollback_waves(root, [("rollback", names)], prev, dry_run)


def rollback_waves(
    root: Path,
    waves: list[tuple[str, tuple[str, ...]]],
    prev: str,
    dry_run: bool,
) -> None:
    """Restore all touched waves, then reactivate them in reverse DAG order."""
    if not waves:
        return
    services = tuple(dict.fromkeys(service for _, names in waves for service in names))
    for name in services:
        restore_runtime(root, name, dry_run)
    paths = unique_paths(
        BUSINESS_COMPOSE_PATHS,
        *(node_paths(name) for name in services if name in NODES),
    )
    checkout(root, prev, paths, dry_run)
    failures: list[str] = []
    for wave, names in rollback_order(waves):
        print(f"cd-compose: ROLLBACK wave={wave} services={','.join(names)} -> {prev}")
        try:
            up_wave_services(root, names, dry_run)
        except ComposeError as exc:
            failures.append(f"{wave}: {exc}")
            print(
                f"cd-compose: 回滚 wave={wave} 时 compose 仍失败: {exc}",
                file=sys.stderr,
            )
    if failures:
        raise ComposeError("cd-compose: 级联回滚失败: " + "; ".join(failures))


def _deploy(root: Path, group: str, dry_run: bool) -> None:
    if group not in NODES:
        known = ", ".join(NODES)
        raise ComposeError(f"cd-compose: 未知节点 {group}（{known}）")
    if not dry_run and not (root / "docker-compose.yml").is_file():
        raise ComposeError(f"cd-compose: 找不到 {root}/docker-compose.yml", 2)
    if dry_run:
        state = {
            "release_sha": "a" * 40,
            "baseline_sha": "b" * 40,
            "identity": "pipeline:dry-run:sha:" + "a" * 40,
        }
        new_sha = state["release_sha"]
        prev_sha = state["baseline_sha"]
    else:
        state, new_sha, prev_sha, _ = release_context()
    if dry_run:
        new_sha = new_sha or "NEW_SHA"
        prev_sha = prev_sha or "PREV_SHA"
    initial_release = False
    paths = (
        ("<initial-release>",)
        if dry_run or initial_release
        else changed_paths(root, prev_sha, new_sha)
    )
    names = node_services(group)
    affected = tuple(name for name in names if name in affected_services(paths))
    if not affected:
        print(f"cd-compose: node={group} 无相关变更，跳过刷新")
        return
    print(f"cd-compose: node={group} new={new_sha} prev={prev_sha} changed={paths}")
    backup_runtime(root, group, dry_run)
    try:
        checkout(root, new_sha, node_paths(group), dry_run)
        activate_services(
            root,
            affected,
            paths,
            dry_run,
            full_refresh=initial_release or "docker-compose.yml" in paths,
        )
    except ComposeError:
        if prev_sha:
            rollback_group(root, group, prev_sha, dry_run)
        raise
    print(f"cd-compose: node={group} 完成")


def deploy(root: Path, group: str, dry_run: bool) -> None:
    with deploy_lock(root):
        _deploy(root, group, dry_run)


def _deploy_waves(root: Path, dry_run: bool) -> None:
    if not dry_run and not (root / "docker-compose.yml").is_file():
        raise ComposeError(f"cd-compose: 找不到 {root}/docker-compose.yml", 2)
    if dry_run:
        state = {
            "release_sha": "a" * 40,
            "baseline_sha": "b" * 40,
            "identity": "pipeline:dry-run:sha:" + "a" * 40,
        }
        new_sha = state["release_sha"]
        prev_sha = state["baseline_sha"]
        initial_release = False
        new_sha = new_sha or "NEW_SHA"
        prev_sha = prev_sha or "PREV_SHA"
    else:
        state, new_sha, prev_sha, _ = release_context()
        initial_release = False
    paths = (
        ("<initial-release>",)
        if dry_run or initial_release
        else changed_paths(root, prev_sha, new_sha)
    )
    affected = set(affected_services(paths))
    full_refresh = initial_release or "docker-compose.yml" in paths
    touched: list[tuple[str, tuple[str, ...]]] = []
    print(f"cd-compose: deployment waves new={new_sha} prev={prev_sha} changed={paths}")
    if not dry_run:
        write_state(
            {
                **state,
                "phase": "deploying",
                "touched_waves": encode_touched_waves(touched),
            },
        )
    for wave, wave_names in WAVES:
        names = tuple(name for name in wave_names if name in affected)
        if not names:
            print(f"cd-compose: wave={wave} 无相关变更，跳过刷新")
            continue
        touched.append((wave, names))
        if not dry_run:
            write_state(
                root,
                {
                    **read_state(),
                    "phase": "deploying",
                    "touched_waves": encode_touched_waves(touched),
                },
            )
        checkout_paths = wave_paths(names)
        print(f"cd-compose: wave={wave} services={','.join(names)}")
        for name in names:
            backup_runtime(root, name, dry_run)
        try:
            if not dry_run:
                assert_transaction_state(state, new_sha, prev_sha)
            checkout(root, new_sha, checkout_paths, dry_run)
            activate_services(root, names, paths, dry_run, full_refresh=full_refresh)
        except ComposeError:
            if prev_sha:
                try:
                    rollback_waves(root, touched, prev_sha, dry_run)
                except ComposeError:
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


def deploy_waves(root: Path, dry_run: bool) -> None:
    """Serialize the complete business deployment on the shared host."""
    with deploy_lock(root):
        _deploy_waves(root, dry_run)


def _rollback_healthcheck(root: Path, dry_run: bool) -> int:
    if dry_run:
        state = {
            "release_sha": "a" * 40,
            "baseline_sha": "b" * 40,
            "identity": "pipeline:dry-run:sha:" + "a" * 40,
        }
        prev_sha = state["baseline_sha"]
        prev_sha = prev_sha or "PREV_SHA"
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


def _mark_healthcheck(root: Path, dry_run: bool) -> None:
    if dry_run:
        print("cd-compose: dry-run，标记 healthcheck_passed")
        return
    with deploy_lock(root):
        state, _, _, _ = release_context()
        if state.get("phase") != "deploying":
            raise ComposeError("cd-compose: 事务不在 deploying 阶段，拒绝标记健康检查")
        write_state({**state, "phase": "healthcheck_passed"})
    print("cd-compose: healthcheck_passed")


def rollback_healthcheck(root: Path, dry_run: bool) -> int:
    with deploy_lock(root):
        return _rollback_healthcheck(root, dry_run)


def mark_healthcheck(root: Path, dry_run: bool) -> None:
    _mark_healthcheck(root, dry_run)


def main() -> int:
    p = argparse.ArgumentParser(description="按部署波次 compose up；失败则回滚该波次")
    p.add_argument(
        "group",
        choices=sorted(NODES) + ["deploy-waves", "healthcheck-rollback", "mark-healthcheck"],
    )
    p.add_argument("--root", default=os.environ.get("INFRA_ROOT", DEFAULT_ROOT))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    try:
        if args.group == "healthcheck-rollback":
            return rollback_healthcheck(Path(args.root), args.dry_run)
        if args.group == "mark-healthcheck":
            mark_healthcheck(Path(args.root), args.dry_run)
            return 0
        if args.group == "deploy-waves":
            deploy_waves(Path(args.root), args.dry_run)
            return 0
        deploy(Path(args.root), args.group, args.dry_run)
    except ComposeError as exc:
        print(exc, file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
