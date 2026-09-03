"""This infra's unit catalog and wave DAG.

The bridge is generic; this module is the only place that names nginx/gitea/grafana.
"""

from __future__ import annotations

from cd.bridge import Unit, action_for, actions_for, unit_is_affected
from cd.docker import ComposeRuntime

DEFAULT_ROOT = "/root/infra"
COMPOSE_PROJECT = "infra"
COMPOSE_FILE = "docker-compose.yml"
FORBIDDEN = frozenset({"woodpecker-server", "woodpecker-agent"})
BUSINESS_COMPOSE_PATHS = ("docker-compose.yml", "compose/")

UNITS: dict[str, Unit] = {
    "gitea": Unit(
        name="gitea",
        paths=("config/", "compose/core.yml"),
        impact_paths=("compose/core.yml",),
        recreate_paths=("compose/core.yml",),
    ),
    "am-config": Unit(
        name="am-config",
        paths=("alertmanager/", "compose/alerting.yml"),
        impact_paths=("alertmanager/", "compose/alerting.yml"),
        runtime=("alertmanager/alertmanager.runtime.yml",),
        oneshot=True,
    ),
    "node_exporter": Unit(
        name="node_exporter",
        paths=("compose/metrics.yml",),
        impact_paths=("compose/metrics.yml",),
        recreate_paths=("compose/metrics.yml",),
    ),
    "loki": Unit(
        name="loki",
        paths=("loki/", "compose/logging.yml"),
        impact_paths=("loki/", "compose/logging.yml"),
        recreate_paths=("loki/", "compose/logging.yml"),
    ),
    "promtail-sd": Unit(
        name="promtail-sd",
        paths=("python/", "promtail/sd.py", "compose/logging.yml"),
        impact_paths=("python/", "promtail/sd.py", "compose/logging.yml"),
        recreate_paths=("promtail/sd.py", "compose/logging.yml"),
        build_paths=("python/",),
    ),
    "alertmanager": Unit(
        name="alertmanager",
        paths=("alertmanager/", "compose/alerting.yml"),
        impact_paths=("alertmanager/", "compose/alerting.yml"),
        runtime=("alertmanager/alertmanager.runtime.yml",),
        rollback_services=("am-config", "alertmanager"),
        reload_paths=("alertmanager/",),
        recreate_paths=("compose/alerting.yml",),
        reload_exec=(("amtool", "check-config", "/etc/alertmanager/alertmanager.yml"),),
        reload_signal="HUP",
    ),
    "promtail": Unit(
        name="promtail",
        paths=("promtail/", "compose/logging.yml"),
        impact_paths=("promtail/", "compose/logging.yml"),
        recreate_paths=("promtail/", "compose/logging.yml"),
    ),
    "prometheus": Unit(
        name="prometheus",
        paths=("prometheus/", "compose/metrics.yml"),
        impact_paths=("prometheus/", "compose/metrics.yml"),
        reload_paths=("prometheus/",),
        recreate_paths=("compose/metrics.yml",),
        reload_exec=(
            ("promtool", "check", "config", "/etc/prometheus/prometheus.yml"),
            ("promtool", "check", "rules", "/etc/prometheus/rules.yml"),
            (
                "wget",
                "-q",
                "--post-data=",
                "http://127.0.0.1:9090/-/reload",
                "-O",
                "/dev/null",
            ),
        ),
    ),
    "grafana": Unit(
        name="grafana",
        paths=("grafana/", "compose/grafana.yml"),
        impact_paths=("grafana/", "compose/grafana.yml"),
        recreate_paths=("grafana/", "compose/grafana.yml"),
    ),
    "nginx": Unit(
        name="nginx",
        paths=("nginx/", "compose/core.yml"),
        impact_paths=("nginx/", "compose/core.yml"),
        reload_paths=("nginx/",),
        recreate_paths=("compose/core.yml",),
        container="nginx-proxy",
        reload_exec=(("nginx", "-t"), ("nginx", "-s", "reload")),
    ),
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


def compose_runtime(root) -> ComposeRuntime:
    return ComposeRuntime(
        root=root,
        project=COMPOSE_PROJECT,
        compose_file=COMPOSE_FILE,
        forbidden=FORBIDDEN,
    )


def units_named(names: tuple[str, ...]) -> tuple[Unit, ...]:
    return tuple(UNITS[name] for name in names)


def service_action(name: str, paths: tuple[str, ...], full_refresh: bool = False) -> str:
    return action_for(UNITS[name], paths, full_refresh)


def service_actions(
    names: tuple[str, ...], paths: tuple[str, ...], full_refresh: bool = False
) -> dict[str, str]:
    return actions_for(units_named(names), paths, full_refresh)


def affected_services(paths: tuple[str, ...]) -> tuple[str, ...]:
    if not paths:
        return ()
    if "<initial-release>" in paths or "docker-compose.yml" in paths:
        return tuple(name for _, names in WAVES for name in names if name in UNITS)
    names: list[str] = []
    for _, wave_names in WAVES:
        for name in wave_names:
            if name in UNITS and unit_is_affected(UNITS[name], paths):
                names.append(name)
    return tuple(names)
