#!/usr/bin/env python3
"""第一期 CI：静态检查仓库，不连接 Docker、不发布。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("缺少 PyYAML。请在 python/requirements.txt 钉上后重新 build infra-python:3.12。")
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]

# 本机 build 的镜像，不要求 digest。
LOCAL_IMAGES = {"infra-python:3.12", "infra-python"}

BUSINESS_COMPOSE_FILES = [
    "docker-compose.yml",
    "compose/core.yml",
    "compose/alerting.yml",
    "compose/metrics.yml",
    "compose/logging.yml",
    "compose/grafana.yml",
]
COMPOSE_FILES = [*BUSINESS_COMPOSE_FILES, "docker-compose.woodpecker.yml"]

YAML_FILES = [
    *COMPOSE_FILES,
    "prometheus/prometheus.yml",
    "prometheus/rules.yml",
    "loki/loki.yml",
    "promtail/promtail.yml",
    "alertmanager/alertmanager.yml.tpl",
    "grafana/provisioning/datasources/prometheus.yml",
    "grafana/provisioning/datasources/loki.yml",
    "grafana/provisioning/dashboards/provider.yml",
    ".woodpecker/woodpecker.yaml",
    ".woodpecker/cd.yaml",
]
YAML_FILE_SET = set(YAML_FILES)

SKIP_SECRET_PARTS = {
    ".git",
    ".env.example",
    "app.ini.example",
}

SECRET_PATTERNS = [
    re.compile(r"BEGIN [A-Z0-9 ]*PRIVATE KEY"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]
# name@sha256:<64 hex>，不要只看有没有 "@sha256:" 这段字。
DIGEST_RE = re.compile(r"@sha256:[0-9a-fA-F]{64}(?![0-9a-fA-F])")

CONFLICT_LEFT = "<" * 7
CONFLICT_RIGHT = ">" * 7

errors: list[str] = []
# rel -> 解析结果；缺文件/冲突/语法错误为 None，避免 compose 再读一遍。
yaml_docs: dict[str, object | None] = {}


def report_conflicts(rel: str, text: str) -> bool:
    if CONFLICT_LEFT in text or CONFLICT_RIGHT in text:
        errors.append(f"{rel}: 含未解决的合并冲突标记")
        return True
    return False


def report_secrets(rel: str, text: str) -> None:
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            errors.append(f"{rel}: 疑似私钥或云密钥，不要入库")
            break


def load_yaml(rel: str) -> object | None:
    if rel in yaml_docs:
        return yaml_docs[rel]
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    if report_conflicts(rel, text):
        yaml_docs[rel] = None
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        errors.append(f"{rel}: YAML 无法解析: {exc}")
        yaml_docs[rel] = None
        return None
    yaml_docs[rel] = data
    return data


def check_yaml_files() -> None:
    for rel in YAML_FILES:
        path = ROOT / rel
        if not path.is_file():
            errors.append(f"缺少文件: {rel}")
            yaml_docs[rel] = None
            continue
        load_yaml(rel)


def image_needs_digest(image: str) -> bool:
    name = image.split("@", 1)[0]
    if name in LOCAL_IMAGES or name.rsplit(":", 1)[0] in LOCAL_IMAGES:
        return False
    return True


def has_digest(image: str) -> bool:
    return DIGEST_RE.search(image) is not None


def _check_service_images(rel: str, services: object) -> None:
    if not isinstance(services, dict):
        return
    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        image = svc.get("image")
        if not image:
            if svc.get("build"):
                continue
            errors.append(f"{rel} 服务 {svc_name}: 没有 image")
            continue
        image_s = str(image)
        if not image_needs_digest(image_s):
            continue
        if has_digest(image_s):
            continue
        errors.append(f"{rel} 服务 {svc_name}: 镜像未钉 digest: {image_s}")


def check_compose_digests() -> None:
    root = yaml_docs.get("docker-compose.yml")
    if not isinstance(root, dict) or not root.get("include"):
        errors.append("docker-compose.yml: 必须通过 include 聚合业务 Compose 文件")

    services_by_name: dict[str, str] = {}
    for rel in BUSINESS_COMPOSE_FILES:
        data = yaml_docs.get(rel)
        if not isinstance(data, dict):
            continue
        services = data.get("services") or {}
        for name in services:
            services_by_name[str(name)] = rel
        for name in ("woodpecker-server", "woodpecker-agent"):
            if name in services:
                errors.append(f"{rel}: {name} 应只出现在 docker-compose.woodpecker.yml")
        _check_service_images(rel, services)
    if "gitea" not in services_by_name or "nginx" not in services_by_name:
        errors.append("业务 Compose 文件: 缺少 gitea 或 nginx 服务")

    wp = yaml_docs.get("docker-compose.woodpecker.yml")
    if isinstance(wp, dict):
        services = wp.get("services") or {}
        if "woodpecker-server" not in services or "woodpecker-agent" not in services:
            errors.append("docker-compose.woodpecker.yml: 缺少 woodpecker-server 或 woodpecker-agent")
        _check_service_images("docker-compose.woodpecker.yml", services)


def skip_secret_path(rel: str) -> bool:
    parts = rel.split("/")
    if ".git" in parts or rel.startswith(".git/"):
        return True
    return any(part in parts or rel.endswith(part) for part in SKIP_SECRET_PARTS)


def check_secrets() -> None:
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if skip_secret_path(rel):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # YAML 清单已在 load_yaml 里报过冲突，这里只补密钥。
        if rel not in YAML_FILE_SET:
            report_conflicts(rel, text)
        report_secrets(rel, text)


def main() -> int:
    check_yaml_files()
    check_compose_digests()
    check_secrets()
    if errors:
        print("CI 失败:")
        for item in errors:
            print(f"  - {item}")
        return 1
    print("CI 通过：YAML 可解析，业务镜像已钉 digest，未见明显密钥入库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
