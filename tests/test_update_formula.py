import hashlib
import io
import json
import tarfile
import unittest
from typing import Any

from scripts.update_formula import (
    FormulaSource,
    UpdateError,
    release_source,
    update_formula,
    verify_source_distribution,
)


def _manifest(version: str = "1.2.3", sha256: str = "a" * 64, size: int = 123) -> bytes:
    return json.dumps(
        {
            "schema": 1,
            "package": "spotter-agent",
            "version": version,
            "release_tag": f"v{version}",
            "artifacts": [
                {
                    "filename": f"spotter_agent-{version}.tar.gz",
                    "kind": "sdist",
                    "sha256": sha256,
                    "size": size,
                }
            ],
        }
    ).encode()


def _release(
    manifest: bytes, version: str = "1.2.3", sha256: str = "a" * 64
) -> dict[str, Any]:
    manifest_name = f"spotter-agent-{version}-release.json"
    sdist_name = f"spotter_agent-{version}.tar.gz"
    return {
        "draft": False,
        "prerelease": False,
        "tag_name": f"v{version}",
        "assets": [
            {
                "name": manifest_name,
                "size": len(manifest),
                "digest": f"sha256:{hashlib.sha256(manifest).hexdigest()}",
                "browser_download_url": f"https://example.test/{manifest_name}",
            },
            {"name": sdist_name, "size": 123, "digest": f"sha256:{sha256}"},
        ],
    }


def _formula(version: str = "1.0.0", sha256: str = "b" * 64) -> str:
    return (
        "class Spotter < Formula\n"
        f'  url "https://github.com/spotter-agent/spotter/releases/download/v{version}/'
        f'spotter_agent-{version}.tar.gz"\n'
        f'  sha256 "{sha256}"\n'
        "\n"
        '  resource "websockets" do\n'
        '    sha256 "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"\n'
        "  end\n"
        "end\n"
    )


def _sdist(
    version: str = "1.2.3",
    requires_python: str = ">=3.11",
    dependencies: tuple[str, ...] = ("websockets<18,>=17",),
) -> bytes:
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: spotter-agent\n"
        f"Version: {version}\n"
        f"Requires-Python: {requires_python}\n"
        + "".join(f"Requires-Dist: {dependency}\n" for dependency in dependencies)
        + "Requires-Dist: pytest<9,>=8; extra == 'dev'\n\n"
    ).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo(f"spotter_agent-{version}/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
    return output.getvalue()


class ReleaseSourceTests(unittest.TestCase):
    def test_accepts_matching_published_release(self) -> None:
        manifest = _manifest()

        source = release_source(_release(manifest), manifest)

        self.assertEqual(source.version, "1.2.3")
        self.assertEqual(source.sha256, "a" * 64)
        self.assertEqual(
            source.url,
            "https://github.com/spotter-agent/spotter/releases/download/"
            "v1.2.3/spotter_agent-1.2.3.tar.gz",
        )

    def test_rejects_draft_and_prerelease(self) -> None:
        manifest = _manifest()
        for field in ("draft", "prerelease"):
            with self.subTest(field=field):
                release = _release(manifest)
                release[field] = True
                with self.assertRaisesRegex(
                    UpdateError, "published and non-prerelease"
                ):
                    release_source(release, manifest)

    def test_rejects_manifest_identity_mismatch(self) -> None:
        manifest = _manifest(version="2.0.0")
        release = _release(manifest, version="1.2.3")

        with self.assertRaisesRegex(UpdateError, "manifest version"):
            release_source(release, manifest)

    def test_rejects_manifest_asset_digest_mismatch(self) -> None:
        manifest = _manifest()
        release = _release(manifest)
        release["assets"][0]["digest"] = f"sha256:{'0' * 64}"

        with self.assertRaisesRegex(UpdateError, "content does not match"):
            release_source(release, manifest)

    def test_rejects_sdist_size_or_digest_mismatch(self) -> None:
        manifest = _manifest()
        for field, value, message in (
            ("size", 999, "size differs"),
            ("digest", f"sha256:{'0' * 64}", "digest differs"),
        ):
            with self.subTest(field=field):
                release = _release(manifest)
                release["assets"][1][field] = value
                with self.assertRaisesRegex(UpdateError, message):
                    release_source(release, manifest)


class FormulaUpdateTests(unittest.TestCase):
    def test_updates_only_the_release_source(self) -> None:
        content = _formula()
        source = FormulaSource(
            "1.2.3",
            "https://github.com/spotter-agent/spotter/releases/download/"
            "v1.2.3/spotter_agent-1.2.3.tar.gz",
            "a" * 64,
        )

        updated, changed = update_formula(content, source)

        self.assertTrue(changed)
        self.assertIn(source.url, updated)
        self.assertIn(f'  sha256 "{source.sha256}"', updated)
        self.assertIn('    sha256 "cccccccccccccccccccccccccccccccc', updated)

    def test_current_release_is_idempotent(self) -> None:
        source = FormulaSource(
            "1.0.0",
            "https://github.com/spotter-agent/spotter/releases/download/"
            "v1.0.0/spotter_agent-1.0.0.tar.gz",
            "b" * 64,
        )
        content = _formula()

        updated, changed = update_formula(content, source)

        self.assertFalse(changed)
        self.assertEqual(updated, content)

    def test_rejects_downgrade(self) -> None:
        source = FormulaSource(
            "0.9.0",
            "https://github.com/spotter-agent/spotter/releases/download/"
            "v0.9.0/spotter_agent-0.9.0.tar.gz",
            "a" * 64,
        )

        with self.assertRaisesRegex(UpdateError, "downgrade"):
            update_formula(_formula(), source)

    def test_rejects_same_version_artifact_mutation(self) -> None:
        source = FormulaSource(
            "1.0.0",
            "https://github.com/spotter-agent/spotter/releases/download/"
            "v1.0.0/spotter_agent-1.0.0.tar.gz",
            "a" * 64,
        )

        with self.assertRaisesRegex(UpdateError, "without a version change"):
            update_formula(_formula(), source)


class SourceDistributionTests(unittest.TestCase):
    def test_accepts_the_pinned_python_and_runtime_dependency_contract(self) -> None:
        content = _sdist()
        source = FormulaSource(
            "1.2.3",
            "https://example.test/spotter.tar.gz",
            hashlib.sha256(content).hexdigest(),
        )

        verify_source_distribution(content, source)

    def test_rejects_download_digest_mismatch(self) -> None:
        source = FormulaSource("1.2.3", "https://example.test/spotter.tar.gz", "0" * 64)

        with self.assertRaisesRegex(UpdateError, "release SHA-256"):
            verify_source_distribution(_sdist(), source)

    def test_rejects_python_or_runtime_dependency_drift(self) -> None:
        for content, message in (
            (_sdist(requires_python=">=3.12"), "Python requirement changed"),
            (
                _sdist(dependencies=("websockets<19,>=18",)),
                "runtime dependencies changed",
            ),
        ):
            with self.subTest(message=message):
                source = FormulaSource(
                    "1.2.3",
                    "https://example.test/spotter.tar.gz",
                    hashlib.sha256(content).hexdigest(),
                )
                with self.assertRaisesRegex(UpdateError, message):
                    verify_source_distribution(content, source)


if __name__ == "__main__":
    unittest.main()
