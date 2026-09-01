#!/usr/bin/env python3
"""Fetch a release SHA and create a transaction against the external baseline.

Does not reset the whole tree: each deploy node checks out its own paths.
docker-compose.yml and docker-compose.woodpecker.yml are checked out to the new SHA.
Does not restart woodpecker-server/agent. Leaves .env / config/app.ini / secrets alone.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from cd_state import (
    current_identity,
    deploy_lock,
    read_baseline,
    read_state,
    validate_sha,
    write_baseline,
    write_state,
)

DEFAULT_ROOT = "/root/infra"
DEFAULT_FETCH_URL = "ssh://git@gitea:22/admin/infra.git"
MARKER_REF = "refs/remotes/gitea-cd/main"
COMPOSE_PATHS = ("docker-compose.yml", "docker-compose.woodpecker.yml", "compose/")


class ApplyError(Exception):
    def __init__(self, msg: str, code: int = 1) -> None:
        super().__init__(msg)
        self.code = code


def git(root: Path, args: list[str], env: dict[str, str] | None = None) -> str:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    cmd = ["git", "-c", f"safe.directory={root}", "-C", str(root), *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, env=run_env)
    except FileNotFoundError as exc:
        raise ApplyError("host-apply: 未找到 git", 2) from exc
    if p.returncode != 0:
        err = (p.stderr or p.stdout or f"exit {p.returncode}").strip()
        raise ApplyError(f"host-apply: git {' '.join(args)} 失败: {err}", 2)
    return p.stdout


def ssh_fetch_env() -> dict[str, str]:
    key = (os.environ.get("SSH_KEY") or "").replace("\r\n", "\n").strip()
    host = (os.environ.get("GITEA_SSH_HOST_KEY") or "").strip()
    if not key or not host:
        raise ApplyError(
            "host-apply: 需要 SSH_KEY 和 GITEA_SSH_HOST_KEY（流水线 secret / WOODPECKER_ENVIRONMENT）",
            2,
        )
    d = Path("/tmp/wp-ssh-cd")
    d.mkdir(mode=0o700, exist_ok=True)
    ident = d / "id"
    ident.write_text(key + "\n", encoding="utf-8")
    ident.chmod(0o600)
    known = d / "known_hosts"
    known.write_text(host + "\n", encoding="utf-8")
    cmd = (
        f"ssh -i {ident} -o IdentitiesOnly=yes "
        f"-o UserKnownHostsFile={known} -o StrictHostKeyChecking=yes"
    )
    return {"GIT_SSH_COMMAND": cmd}


def have_commit(root: Path, sha: str) -> bool:
    try:
        git(root, ["cat-file", "-e", f"{sha}^{{commit}}"])
    except ApplyError:
        return False
    return True


def fetch_sha(root: Path, sha: str, fetch_url: str) -> None:
    env = ssh_fetch_env()
    git(root, ["fetch", "--depth=50", fetch_url, f"+refs/heads/main:{MARKER_REF}"], env=env)
    if not have_commit(root, sha):
        print("host-apply: SHA 不在已 fetch 的 main 里，再按对象拉一次")
        git(root, ["fetch", "--depth=50", fetch_url, sha], env=env)
    if not have_commit(root, sha):
        raise ApplyError(f"host-apply: 远端没有提交 {sha}", 2)


def apply(root: Path, sha: str, fetch_url: str, dry_run: bool) -> None:
    if not sha:
        raise ApplyError("host-apply: 缺少 CI_COMMIT_SHA", 2)
    try:
        sha = validate_sha(sha, "CI_COMMIT_SHA")
    except ValueError as exc:
        raise ApplyError(f"host-apply: {exc}", 2) from exc
    print(f"host-apply: root={root} sha={sha}")
    if dry_run:
        print("host-apply: dry-run，不改宿主机树")
        return
    if not root.is_dir():
        raise ApplyError(f"host-apply: 宿主机目录不存在: {root}", 2)
    git(root, ["rev-parse", "--is-inside-work-tree"])
    with deploy_lock(root):
        fetch_sha(root, sha, fetch_url)
        main_sha = git(root, ["rev-parse", MARKER_REF]).strip().lower()
        if main_sha != sha:
            raise ApplyError(
                f"host-apply: release {sha} 不是当前 main {main_sha}，拒绝旧流水线 Retry",
                2,
            )
        try:
            baseline = read_baseline()
        except (OSError, ValueError) as exc:
            raise ApplyError(f"host-apply: 外置 baseline 无效: {exc}", 2) from exc
        baseline_sha = baseline["success_sha"]
        if not have_commit(root, baseline_sha):
            raise ApplyError(f"host-apply: baseline 提交不存在: {baseline_sha}", 2)
        identity = current_identity()
        prior = read_state()
        prior_release = str(prior.get("release_sha") or "")
        prior_baseline = str(prior.get("baseline_sha") or "")
        prior_phase = str(prior.get("phase") or "")
        if prior_phase in {"deploying", "rollback_failed", "commit_pending"}:
            if prior_release != sha or prior_baseline != baseline_sha:
                raise ApplyError("host-apply: 存在未完成且不匹配的发布事务，拒绝覆盖", 2)
        elif prior_phase == "succeeded" and prior_release != sha:
            raise ApplyError("host-apply: 上一事务已成功，拒绝旧流水线 Retry", 2)
        transaction = {
            "version": 1,
            "phase": "deploying",
            "release_sha": sha.lower(),
            "baseline_sha": baseline_sha,
            "identity": identity,
            "touched_waves": "[]",
        }
        write_state(transaction)
        git(root, ["checkout", sha, "--", *COMPOSE_PATHS])
    print(f"host-apply: baseline={baseline_sha} release={sha} 已创建外置发布事务，compose 文件对齐新 SHA")
    print("host-apply: 不 reset 整棵树；各 deploy 节点自己 checkout 路径")


def finalize(root: Path, dry_run: bool) -> None:
    data = read_state()
    sha = str(data.get("release_sha") or os.environ.get("CI_COMMIT_SHA", "")).strip()
    baseline_sha = str(data.get("baseline_sha") or "").strip()
    if not sha:
        raise ApplyError("host-apply --finalize: 没有 new_sha", 2)
    if not baseline_sha:
        raise ApplyError("host-apply --finalize: 没有固定 baseline_sha", 2)
    try:
        sha = validate_sha(sha, "release_sha")
        baseline_sha = validate_sha(baseline_sha, "baseline_sha")
        if dry_run:
            baseline = {"success_sha": baseline_sha}
            identity = str(data.get("identity") or "")
        else:
            baseline = read_baseline()
            identity = current_identity()
    except (OSError, ValueError) as exc:
        raise ApplyError(f"host-apply --finalize: 外置状态无效: {exc}", 2) from exc
    if baseline["success_sha"] != baseline_sha:
        raise ApplyError("host-apply --finalize: baseline 已变化，拒绝提交旧事务", 2)
    if str(data.get("identity") or "") != identity:
        raise ApplyError("host-apply --finalize: 发布身份不匹配", 2)
    if not dry_run and str(data.get("phase") or "") != "healthcheck_passed":
        raise ApplyError("host-apply --finalize: 健康检查尚未通过", 2)
    print(f"host-apply: finalize HEAD -> {sha}")
    if dry_run:
        print("host-apply: dry-run，不 reset")
        return
    with deploy_lock(root):
        data = read_state()
        if str(data.get("release_sha") or "").lower() != sha:
            raise ApplyError("host-apply --finalize: 发布事务已被替换", 2)
        if str(data.get("baseline_sha") or "").lower() != baseline_sha:
            raise ApplyError("host-apply --finalize: 发布基线已被替换", 2)
        if str(data.get("identity") or "") != identity:
            raise ApplyError("host-apply --finalize: 发布身份已被替换", 2)
        if str(data.get("phase") or "") != "healthcheck_passed":
            raise ApplyError("host-apply --finalize: 健康检查状态已被替换", 2)
        try:
            if read_baseline()["success_sha"] != baseline_sha:
                raise ApplyError("host-apply --finalize: baseline 已在锁内变化", 2)
        except (OSError, ValueError) as exc:
            if isinstance(exc, ApplyError):
                raise
            raise ApplyError(f"host-apply --finalize: 锁内读取 baseline 失败: {exc}", 2) from exc
        git(root, ["reset", "--hard", sha])
        head = git(root, ["rev-parse", "HEAD"]).strip()
        if head != sha:
            raise ApplyError(f"host-apply: finalize 后 HEAD={head}，期望 {sha}", 2)
        write_state({**data, "phase": "commit_pending"})
        try:
            write_baseline(sha)
        except (OSError, ValueError) as exc:
            raise ApplyError(f"host-apply --finalize: 更新 baseline 失败: {exc}", 2) from exc
        write_state(
            {
                **data,
                "phase": "succeeded",
                "release_sha": sha,
                "baseline_sha": sha,
            }
        )
    print(f"host-apply: baseline 已原子更新为 {sha}")


def main() -> int:
    p = argparse.ArgumentParser(description="抓取流水线 SHA，并记录 CD 回滚用的上一成功版本")
    p.add_argument("--root", default=os.environ.get("INFRA_ROOT", DEFAULT_ROOT))
    p.add_argument("--sha", default=os.environ.get("CI_COMMIT_SHA", ""))
    p.add_argument("--fetch-url", default=os.environ.get("CD_FETCH_URL", DEFAULT_FETCH_URL))
    p.add_argument("--finalize", action="store_true", help="healthcheck 通过后：整树 reset 到 new SHA")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    try:
        if args.finalize:
            finalize(Path(args.root), args.dry_run)
        else:
            apply(Path(args.root), args.sha.strip(), args.fetch_url, args.dry_run)
    except ApplyError as exc:
        print(exc, file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
