#!/usr/bin/env python3
"""给 Promtail 生成 file_sd：只收指定容器。读磁盘上的 Docker 元数据，不挂 docker.sock。"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

CONTAINERS = Path("/var/lib/docker/containers")
OUT = Path("/etc/promtail/sd/containers.json")
ALLOW = {
    "nginx-proxy": "nginx",
    "gitea": "gitea",
    "woodpecker-server": "woodpecker-server",
    "woodpecker-agent": "woodpecker-agent",
}


def targets() -> list[dict]:
    rows: list[dict] = []
    if not CONTAINERS.is_dir():
        return rows
    for cfg in CONTAINERS.glob("*/config.v2.json"):
        try:
            meta = json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = str(meta.get("Name", "")).lstrip("/")
        job = ALLOW.get(name)
        if job is None:
            continue
        log = cfg.with_name(f"{cfg.parent.name}-json.log")
        if not log.is_file():
            continue
        rows.append(
            {
                "targets": ["localhost"],
                "labels": {
                    "job": job,
                    "container": name,
                    "__path__": str(log),
                },
            }
        )
    rows.sort(key=lambda row: row["labels"]["container"])
    return rows


def write_sd() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(targets(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0, metavar="SEC")
    args = parser.parse_args()
    write_sd()
    while args.loop > 0:
        time.sleep(args.loop)
        write_sd()


if __name__ == "__main__":
    main()
