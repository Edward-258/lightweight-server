#!/usr/bin/env python3
"""CI policy：按 feat/fix 前缀限制路径。越界须在说明里点名。

  python scripts/check_scope.py --branch feat/nginx-foo --names nginx/nginx.conf
  python scripts/check_scope.py --branch feat/nginx-foo --names docker-compose.yml --message msg.txt
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# feat|fix/<key>-* ；chore 仅 ci。目录以 / 结尾；*.md 表示任意 markdown。
SCOPES: dict[str, tuple[str, ...]] = {
    "nginx": ("nginx/",),
    "prometheus": ("prometheus/",),
    "alerting": ("alertmanager/", "prometheus/rules.yml"),
    "grafana": ("grafana/",),
    "loki": ("loki/",),
    "promtail": ("promtail/",),
    "compose": ("docker-compose.yml", "docker-compose.woodpecker.yml", "compose/"),
    "python": ("python/",),
    "gitea": ("config/",),
    "ci": ("python/", "scripts/", ".woodpecker.yaml", ".woodpecker/"),
    "healthcheck": ("scripts/healthcheck.py", ".woodpecker.yaml", ".woodpecker/", "scripts/README.md"),
    "docs": ("*.md",),
}

HINT = (
    "ci-scope: cross-scope\n"
    "- docker-compose.yml: 原因\n"
    "路径须为 infra 下完整相对路径（可带盘符或 infra/ 前缀；中英文冒号均可）。"
)
MARKER = re.compile(r"^ci-scope\s*[:：]\s*cross-scope\s*$", re.I)
BRANCH_RE = re.compile(r"^(feat|fix|chore)/([a-z]+)(?:-|$)")


class PolicyError(Exception):
    def __init__(self, msg: str, code: int = 1) -> None:
        super().__init__(msg)
        self.code = code


def posix(path: str) -> str:
    rel = path.replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(p for p in items if p))


def normalize_branch(name: str) -> str:
    name = name.strip()
    for prefix in ("refs/heads/", "origin/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return name


def rules_for(branch: str) -> tuple[str, ...] | None:
    m = BRANCH_RE.match(branch)
    if not m:
        return None
    kind, area = m.groups()
    if kind == "chore" and area != "ci":
        return None
    return SCOPES.get(area)


def allowed(rel: str, rules: tuple[str, ...]) -> bool:
    rel = posix(rel)
    for rule in rules:
        if rule == "*.md" and rel.lower().endswith(".md"):
            return True
        if rule.endswith("/") and (rel == rule[:-1] or rel.startswith(rule)):
            return True
        if rel == rule:
            return True
    return False


def covers(written: str, actual: str) -> bool:
    w, a = posix(written), posix(actual)
    return bool(w and a) and (w == a or w.endswith("/" + a))


def split_path_reason(body: str) -> tuple[str, str] | None:
    body = body.strip()
    at = None
    for m in re.finditer(r"[:：]", body):
        i = m.start()
        if i == 1 and body[0].isalpha() and len(body) > 2 and body[2] in r"\/":
            continue
        at = i
    if at is None:
        return None
    path, reason = body[:at].strip(), body[at + 1 :].strip()
    return (path, reason) if path and reason and len(path) > 1 else None


def parse_exceptions(text: str) -> list[tuple[str, str]]:
    entries, bad, block = [], [], False
    for raw in text.splitlines():
        s = raw.strip()
        if MARKER.match(s):
            block = True
            continue
        if not block:
            continue
        if not s or s.lower().startswith("ci-scope"):
            block = bool(MARKER.match(s))
            continue
        item = re.match(r"^-\s+(.+)$", s)
        if not item:
            block = False
            continue
        parsed = split_path_reason(item.group(1))
        (bad if parsed is None else entries).append(parsed or s)
    if bad:
        raise PolicyError("policy: ci-scope 行无法解析（需要「- 路径: 原因」）:\n" + "\n".join(f"  {x}" for x in bad))
    return entries


def parse_names(text: str) -> list[str]:
    lines = text.splitlines()
    patch = []
    for line in lines:
        parts = line.split()
        if line.startswith("diff --git ") and len(parts) >= 4:
            b = parts[3]
            patch.append(b[2:] if b.startswith("b/") else b)
    src = patch or [
        ln.strip()
        for ln in lines
        if ln.strip() and not ln.strip().startswith(("#", "+++", "---", "@@", "index "))
    ]
    return unique(posix(n) for n in src if posix(n) not in {"", "/dev/null"})


def git(args: list[str], required: bool = True, env: dict[str, str] | None = None) -> str | None:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    try:
        p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, env=run_env)
    except FileNotFoundError:
        if not required:
            return None
        raise PolicyError("policy: 未找到 git", 2) from None
    if p.returncode == 0:
        return p.stdout
    if not required:
        return None
    err = (p.stderr or p.stdout or f"exit {p.returncode}").strip()
    raise PolicyError(f"policy: git {' '.join(args)} 失败: {err}", 2)


def detect_branch(explicit: str | None) -> str:
    if explicit:
        return normalize_branch(explicit)
    event = os.environ.get("CI_PIPELINE_EVENT", "")
    keys = (
        ("CI_COMMIT_SOURCE_BRANCH", "CI_COMMIT_BRANCH")
        if os.environ.get("CI_COMMIT_PULL_REQUEST") or event == "pull_request"
        else ("CI_COMMIT_BRANCH", "CI_COMMIT_REF")
    )
    for key in keys:
        if os.environ.get(key):
            return normalize_branch(os.environ[key])
    head = (git(["rev-parse", "--abbrev-ref", "HEAD"], required=False) or "").strip()
    if head and head != "HEAD":
        return normalize_branch(head)
    raise PolicyError("policy: 无法确定分支名，请传 --branch", 2)


def ssh_fetch_env() -> dict[str, str] | None:
    key = (os.environ.get("SSH_KEY") or "").replace("\r\n", "\n").strip()
    host = (os.environ.get("GITEA_SSH_HOST_KEY") or "").strip()
    if not key or not host:
        return None
    d = Path("/tmp/wp-ssh")
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


def ensure_origin_main() -> None:
    if git(["rev-parse", "--verify", "origin/main^{commit}"], required=False) is not None:
        return
    env = ssh_fetch_env()
    if not env:
        return
    print("policy: fetching origin/main")
    git(["fetch", "--depth=50", "origin", "+refs/heads/main:refs/remotes/origin/main"], env=env)


def detect_base() -> str:
    # plugin-git 会 git init -b <CI 分支> 再 reset 到本次 SHA。
    # 流水线里的本地 main 常常就是当前提交，不能当对比基线。
    in_ci = bool(os.environ.get("CI") or os.environ.get("CI_PIPELINE_EVENT"))
    if in_ci:
        ensure_origin_main()
    target = normalize_branch(os.environ.get("CI_COMMIT_TARGET_BRANCH") or "main")
    names = [f"origin/{target}", "origin/main"]
    if not in_ci:
        names.extend([target, "main"])
    for cand in names:
        if cand and git(["rev-parse", "--verify", f"{cand}^{{commit}}"], required=False) is not None:
            return cand
    raise PolicyError(
        "policy: 找不到 origin/main，无法对比范围。push 时 clone 不会带 main，policy 需 SSH_KEY 去 fetch。",
        2,
    )


def collect_names(diff_text: str | None, extra: list[str]) -> list[str]:
    names = [posix(x) for x in extra if x.strip()]
    if diff_text:
        names.extend(parse_names(diff_text))
    if names:
        return unique(names)
    return parse_names(
        git(["diff", "--name-only", "--diff-filter=ACMRD", f"{detect_base()}...HEAD", "--"]) or ""
    )


def collect_messages(files: list[str], texts: list[str]) -> str:
    parts = list(texts) + [Path(f).read_text(encoding="utf-8") for f in files]
    if os.environ.get("CI_COMMIT_MESSAGE"):
        parts.append(os.environ["CI_COMMIT_MESSAGE"])
    log = git(["log", "--format=%B", f"{detect_base()}...HEAD"], required=False)
    if log:
        parts.append(log)
    return "\n".join(parts)


def run(args: argparse.Namespace) -> int:
    event = os.environ.get("CI_PIPELINE_EVENT", "local")
    branch = detect_branch(args.branch)
    print(f"policy: branch={branch} event={event}")
    if branch in {"main", "master"}:
        print(f"policy: 分支 {branch} 跳过范围检查")
        return 0

    rules = rules_for(branch)
    if rules is None:
        keys = ",".join(SCOPES)
        raise PolicyError(f"policy: 不支持的分支名: {branch}\n请使用 feat|fix/{{{keys}}}-* 或 chore/ci-*。")

    text = Path(args.diff).read_text(encoding="utf-8") if args.diff else None
    changed = collect_names(text, args.names)
    if not changed:
        print(f"policy: 分支 {branch} 没有检测到文件改动")
        return 0

    exceptions = parse_exceptions(collect_messages(args.message, args.message_text))
    undeclared = []
    for rel in changed:
        if allowed(rel, rules):
            continue
        hits = [why for written, why in exceptions if covers(written, rel)]
        if hits:
            print(f"policy: 越界已声明 {rel}")
            for why in hits:
                print(f"  - {why}")
        else:
            undeclared.append(rel)
    if undeclared:
        listed = "\n".join(f"  - {p}" for p in undeclared)
        raise PolicyError(f"policy: 分支 {branch} 存在未声明的越界文件:\n{listed}\n{HINT}")
    print(f"policy: 分支 {branch} 范围检查通过（{len(changed)} 个文件）")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="按 feat/fix 前缀检查本次改动是否越界")
    p.add_argument("--branch")
    p.add_argument("--diff")
    p.add_argument("--names", nargs="*", default=[])
    p.add_argument("--message", action="append", default=[])
    p.add_argument("--message-text", action="append", default=[])
    try:
        return run(p.parse_args())
    except PolicyError as exc:
        print(exc, file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
