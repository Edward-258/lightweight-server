#!/usr/bin/env python3
"""Pure tests for CD change impact and refresh action selection."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cd_compose import (
    affected_services,
    decode_touched_waves,
    encode_touched_waves,
    rollback_order,
    service_action,
    service_actions,
)


class RefreshDecisionTests(unittest.TestCase):
    def test_unrelated_changes_do_not_affect_business_services(self) -> None:
        self.assertEqual(affected_services(("scripts/README.md",)), ())

    def test_component_impact_is_scoped(self) -> None:
        self.assertEqual(affected_services(("nginx/nginx.conf",)), ("nginx",))
        self.assertEqual(affected_services(("prometheus/rules.yml",)), ("prometheus",))
        self.assertEqual(
            affected_services(("compose/metrics.yml",)),
            ("node_exporter", "prometheus"),
        )

    def test_reload_services(self) -> None:
        self.assertEqual(service_action("nginx", ("nginx/nginx.conf",)), "reload")
        self.assertEqual(
            service_action("prometheus", ("prometheus/prometheus.yml",)), "reload"
        )
        self.assertEqual(
            service_action("alertmanager", ("alertmanager/alertmanager.yml.tpl",)),
            "reload",
        )
        self.assertEqual(
            service_action("am-config", ("alertmanager/alertmanager.yml.tpl",)),
            "oneshot",
        )

    def test_recreate_services(self) -> None:
        self.assertEqual(service_action("loki", ("loki/loki.yml",)), "recreate")
        self.assertEqual(
            service_action("promtail", ("promtail/promtail.yml",)), "recreate"
        )
        self.assertEqual(
            service_action("grafana", ("grafana/provisioning/dashboards/provider.yml",)),
            "recreate",
        )

    def test_build_context_requires_build_and_recreate(self) -> None:
        self.assertEqual(
            service_action("promtail-sd", ("python/Dockerfile",)), "build+recreate"
        )
        self.assertEqual(
            service_action("promtail-sd", ("promtail/sd.py",)), "recreate"
        )
        self.assertEqual(
            service_action("promtail-sd", ("<initial-release>",), full_refresh=True),
            "build+recreate",
        )

    def test_compose_definition_requires_recreate(self) -> None:
        actions = service_actions(
            ("nginx", "gitea"), ("compose/core.yml",)
        )
        self.assertEqual(actions, {"nginx": "recreate", "gitea": "recreate"})

    def test_root_compose_change_affects_all_business_services(self) -> None:
        names = affected_services(("docker-compose.yml",))
        self.assertEqual(
            names,
            (
                "gitea",
                "nginx",
                "am-config",
                "alertmanager",
                "node_exporter",
                "loki",
                "promtail-sd",
                "prometheus",
                "promtail",
                "grafana",
            ),
        )

    def test_initial_release_contains_only_business_services(self) -> None:
        names = affected_services(("<initial-release>",))
        self.assertNotIn("woodpecker-server", names)
        self.assertNotIn("woodpecker-agent", names)
        self.assertIn("promtail-sd", names)

    def test_touched_wave_state_round_trips(self) -> None:
        waves = [("core", ("gitea",)), ("dependent", ("prometheus", "promtail"))]
        self.assertEqual(decode_touched_waves(encode_touched_waves(waves)), waves)

    def test_rollback_is_reverse_and_deduplicated(self) -> None:
        waves = [
            ("core", ("gitea",)),
            ("alerting", ("alertmanager",)),
            ("dependent", ("prometheus",)),
            ("alerting", ("alertmanager",)),
        ]
        self.assertEqual(
            rollback_order(waves),
            [
                ("alerting", ("alertmanager",)),
                ("dependent", ("prometheus",)),
                ("core", ("gitea",)),
            ],
        )

    def test_state_identity_is_part_of_the_release(self) -> None:
        waves = [("core", ("gitea",))]
        encoded = encode_touched_waves(waves)
        self.assertEqual(decode_touched_waves(encoded), waves)


if __name__ == "__main__":
    unittest.main()
