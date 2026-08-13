import hashlib
import tempfile
import unittest
from pathlib import Path

from scripts.lifecycle_smoke import LifecycleSmokeError, _formula_text


class LifecycleSmokeTests(unittest.TestCase):
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
