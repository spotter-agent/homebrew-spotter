import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lifecycle_smoke import (
    LifecycleSmokeError,
    RuntimeStatus,
    _formula_text,
    _runtime_status,
)


class LifecycleSmokeTests(unittest.TestCase):
    def test_runtime_status_ignores_fields_after_build_identity(self) -> None:
        output = (
            "spotterd: healthy (pid=5320, protocol=3, version=9.98.0, "
            "build=v9.98.0@81501299, config=cfg-123, "
            "compatibility=compatible_stale, runtime=runtime-123)\n"
        )
        completed = subprocess.CompletedProcess(
            ["spotter", "daemon", "status"], 0, output, ""
        )

        with patch("scripts.lifecycle_smoke._run", return_value=completed):
            status = _runtime_status(Path("spotter"), {})

        self.assertEqual(RuntimeStatus(5320, "9.98.0", "v9.98.0@81501299"), status)

    def test_fixture_formula_reuses_the_published_formula_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "spotter_agent-9.99.0.tar.gz"
            artifact.write_bytes(b"fixture")
            template = Path("Formula/spotter.rb").read_text()

            generated = _formula_text(template, artifact, "9.99.0")

            self.assertIn("class SpotterLifecycle < Formula", generated)
            self.assertIn(f'url "{artifact.resolve().as_uri()}"', generated)
            self.assertIn('version "9.99.0"', generated)
            self.assertIn(hashlib.sha256(b"fixture").hexdigest(), generated)
            self.assertIn('keep_alive path: opt_bin/"spotterd"', generated)
            self.assertNotIn("releases/download/v0.0.1", generated)

    def test_fixture_formula_refuses_an_unrecognized_source_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "spotter_agent-9.99.0.tar.gz"
            artifact.write_bytes(b"fixture")

            with self.assertRaisesRegex(LifecycleSmokeError, "source URL/checksum"):
                _formula_text("class Spotter < Formula\nend\n", artifact, "9.99.0")
