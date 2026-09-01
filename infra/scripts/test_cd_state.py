#!/usr/bin/env python3
"""Pure tests for the external baseline state helpers."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cd_state


class ExternalStateTests(unittest.TestCase):
    def test_baseline_is_atomic_and_strictly_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CD_STATE_ROOT": directory}):
                sha = "A" * 40
                cd_state.write_baseline(sha)
                self.assertEqual(cd_state.read_baseline(), {"success_sha": "a" * 40})
                data = json.loads((Path(directory) / "baseline.json").read_text())
                self.assertEqual(data["success_sha"], "a" * 40)

    def test_invalid_baseline_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CD_STATE_ROOT": directory}):
                path = Path(directory) / "baseline.json"
                path.write_text('{"success_sha":"not-a-commit"}')
                with self.assertRaises(ValueError):
                    cd_state.read_baseline()

    def test_write_state_is_external(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"CD_STATE_ROOT": directory}):
                cd_state.write_state({"phase": "deploying"})
                self.assertEqual(cd_state.read_state(), {"phase": "deploying"})

    def test_sha_validation_is_strict(self) -> None:
        self.assertEqual(cd_state.validate_sha("A" * 40), "a" * 40)
        with self.assertRaises(ValueError):
            cd_state.validate_sha("not-a-sha", "baseline_sha")


if __name__ == "__main__":
    unittest.main()
