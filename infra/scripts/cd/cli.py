"""Argparse entry for the CD compose control plane."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cd.catalog import DEFAULT_ROOT, UNITS
from cd.errors import CdError
from cd.orchestrate import deploy, mark_healthcheck, rollback_healthcheck

OLD_COMMANDS = {
    "deploy-waves": ("deploy",),
    "healthcheck-rollback": ("rollback", "--healthcheck"),
    "mark-healthcheck": ("mark-healthcheck",),
}

FLAGS_WITH_VALUE = {"--root"}


def normalize_argv(argv: list[str]) -> list[str]:
    """Rewrite legacy positional verbs/unit names into subcommands."""
    i = 1
    while i < len(argv):
        token = argv[i]
        if token in {"-h", "--help"}:
            return argv
        if token.startswith("-"):
            if token in FLAGS_WITH_VALUE and i + 1 < len(argv):
                i += 2
                continue
            i += 1
            continue
        if token in OLD_COMMANDS:
            return argv[:i] + list(OLD_COMMANDS[token]) + argv[i + 1 :]
        if token in UNITS:
            return argv[:i] + ["deploy", token] + argv[i + 1 :]
        return argv
    return argv


def build_parser() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--root", default=os.environ.get("INFRA_ROOT", DEFAULT_ROOT))
    parent.add_argument("--dry-run", action="store_true")

    parser = argparse.ArgumentParser(
        description="按波次或单元发布业务 compose；失败则回滚已触碰波次",
        parents=[parent],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    deploy_p = sub.add_parser("deploy", parents=[parent], help="发布受影响单元")
    deploy_p.add_argument(
        "units",
        nargs="*",
        help="只发布这些单元（仍按 DAG 波次排序）；默认按 git diff 选择",
    )
    deploy_p.add_argument(
        "--wave",
        action="append",
        dest="waves",
        default=[],
        metavar="WAVE",
        help="只跑指定波次，可重复",
    )

    rollback_p = sub.add_parser("rollback", parents=[parent], help="回滚已触碰波次")
    rollback_p.add_argument(
        "--healthcheck",
        action="store_true",
        help="健康检查失败后回滚",
    )

    sub.add_parser("mark-healthcheck", parents=[parent], help="标记事务 healthcheck_passed")
    return parser


def dispatch(args: argparse.Namespace) -> int:
    root = Path(args.root)
    if args.command == "deploy":
        only_units = tuple(args.units) or None
        only_waves = tuple(args.waves) or None
        deploy(root, args.dry_run, only_units, only_waves)
        return 0
    if args.command == "rollback":
        if not args.healthcheck:
            raise CdError("cd-compose: rollback 目前只支持 --healthcheck")
        return rollback_healthcheck(root, args.dry_run)
    if args.command == "mark-healthcheck":
        mark_healthcheck(root, args.dry_run)
        return 0
    raise CdError(f"cd-compose: 未知命令 {args.command}")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv if argv is None else argv)
    normalized = normalize_argv(raw)
    parser = build_parser()
    try:
        args = parser.parse_args(normalized[1:])
        return dispatch(args)
    except CdError as exc:
        print(exc, file=sys.stderr)
        return exc.code
