#!/usr/bin/env python3
"""CD CLI: path-scoped compose up. Implementation lives in the cd package.

  python3 scripts/cd_compose.py deploy
  python3 scripts/cd_compose.py deploy nginx prometheus
  python3 scripts/cd_compose.py deploy --wave alerting
  python3 scripts/cd_compose.py rollback --healthcheck
  python3 scripts/cd_compose.py mark-healthcheck

Legacy verbs still work: deploy-waves, healthcheck-rollback, <unit-name>.
Never deploy woodpecker-server/agent.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from cd.catalog import affected_services, service_action, service_actions
from cd.cli import main
from cd.errors import CdError
from cd.orchestrate import decode_touched_waves, encode_touched_waves, rollback_order

ComposeError = CdError

__all__ = [
    "ComposeError",
    "affected_services",
    "decode_touched_waves",
    "encode_touched_waves",
    "main",
    "rollback_order",
    "service_action",
    "service_actions",
]


if __name__ == "__main__":
    sys.exit(main())
