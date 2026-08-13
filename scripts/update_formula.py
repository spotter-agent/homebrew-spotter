#!/usr/bin/env python3
"""Update the Spotter Formula from a verified, published GitHub Release."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Any, cast

RELEASE_API = "https://api.github.com/repos/spotter-agent/spotter/releases/latest"
RELEASE_BASE = "https://github.com/spotter-agent/spotter/releases/download"
TAG_PATTERN = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SOURCE_PATTERN = re.compile(
    r'(?m)^  url "https://github\.com/spotter-agent/spotter/releases/download/'
    r'v(?P<version>\d+\.\d+\.\d+)/spotter_agent-(?P=version)\.tar\.gz"\n'
    r'  sha256 "(?P<sha256>[0-9a-f]{64})"$'
)
EXPECTED_REQUIRES_PYTHON = ">=3.11"
EXPECTED_RUNTIME_DEPENDENCIES = ["websockets<18,>=17"]


class UpdateError(RuntimeError):
    """The upstream release cannot safely update the Formula."""


@dataclass(frozen=True)
class FormulaSource:
    version: str
    url: str
    sha256: str


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise UpdateError(f"{name} must be an object")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise UpdateError(f"{name} must be a non-empty string")
    return value


def _version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        raise UpdateError(f"invalid semantic version {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _asset_map(release: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list):
        raise UpdateError("release assets must be an array")
    assets: dict[str, Mapping[str, Any]] = {}
    for raw in raw_assets:
        asset = _object(raw, "release asset")
        name = _text(asset.get("name"), "release asset name")
        if name in assets:
            raise UpdateError(f"duplicate release asset {name}")
        assets[name] = asset
    return assets


def _verify_asset_digest(asset: Mapping[str, Any], content: bytes, name: str) -> None:
    digest = asset.get("digest")
    if digest is None:
        return
    expected = f"sha256:{hashlib.sha256(content).hexdigest()}"
    if digest != expected:
        raise UpdateError(f"{name} content does not match its GitHub asset digest")


def release_source(release: object, manifest_content: bytes) -> FormulaSource:
    release_data = _object(release, "release")
    if (
        release_data.get("draft") is not False
        or release_data.get("prerelease") is not False
    ):
        raise UpdateError("latest release must be published and non-prerelease")

    tag = _text(release_data.get("tag_name"), "release tag")
    tag_match = TAG_PATTERN.fullmatch(tag)
    if tag_match is None:
        raise UpdateError(f"release tag {tag!r} is not vMAJOR.MINOR.PATCH")
    version = tag_match.group("version")
    assets = _asset_map(release_data)

    manifest_name = f"spotter-agent-{version}-release.json"
    manifest_asset = assets.get(manifest_name)
    if manifest_asset is None:
        raise UpdateError(f"release is missing {manifest_name}")
    _verify_asset_digest(manifest_asset, manifest_content, manifest_name)
    try:
        manifest = _object(json.loads(manifest_content), "release manifest")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError(f"release manifest is not valid JSON: {error}") from error

    expected = {
        "schema": 1,
        "package": "spotter-agent",
        "version": version,
        "release_tag": tag,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise UpdateError(
                f"release manifest {key} does not match the published release"
            )

    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise UpdateError("release manifest artifacts must be an array")
    sdist_name = f"spotter_agent-{version}.tar.gz"
    sdists = [
        _object(item, "release artifact")
        for item in raw_artifacts
        if isinstance(item, dict) and item.get("kind") == "sdist"
    ]
    if len(sdists) != 1 or sdists[0].get("filename") != sdist_name:
        raise UpdateError(
            "release manifest must identify exactly one expected source distribution"
        )
    sdist = sdists[0]
    sha256 = _text(sdist.get("sha256"), "source distribution SHA-256")
    if SHA256_PATTERN.fullmatch(sha256) is None:
        raise UpdateError("source distribution SHA-256 is malformed")
    size = sdist.get("size")
    if not isinstance(size, int) or size <= 0:
        raise UpdateError("source distribution size must be a positive integer")

    release_sdist = assets.get(sdist_name)
    if release_sdist is None:
        raise UpdateError(f"release is missing {sdist_name}")
    if release_sdist.get("size") != size:
        raise UpdateError(
            "source distribution size differs between manifest and release asset"
        )
    asset_digest = release_sdist.get("digest")
    if asset_digest is not None and asset_digest != f"sha256:{sha256}":
        raise UpdateError(
            "source distribution digest differs between manifest and release asset"
        )

    return FormulaSource(version, f"{RELEASE_BASE}/{tag}/{sdist_name}", sha256)


def verify_source_distribution(content: bytes, source: FormulaSource) -> None:
    """Verify the downloaded package and its Formula-facing runtime contract."""
    if hashlib.sha256(content).hexdigest() != source.sha256:
        raise UpdateError(
            "downloaded source distribution does not match the release SHA-256"
        )
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            metadata_names = [
                name for name in archive.getnames() if name.endswith("/PKG-INFO")
            ]
            if len(metadata_names) != 1:
                raise UpdateError(
                    "source distribution must contain exactly one PKG-INFO"
                )
            metadata_file = archive.extractfile(metadata_names[0])
            if metadata_file is None:
                raise UpdateError("source distribution package metadata is unreadable")
            metadata = Parser().parsestr(metadata_file.read().decode())
    except (tarfile.TarError, UnicodeDecodeError) as error:
        raise UpdateError(f"source distribution is invalid: {error}") from error

    if (
        metadata.get("Name") != "spotter-agent"
        or metadata.get("Version") != source.version
    ):
        raise UpdateError(
            "source distribution package identity differs from the release"
        )
    if metadata.get("Requires-Python") != EXPECTED_REQUIRES_PYTHON:
        raise UpdateError(
            "Spotter's Python requirement changed; update the Formula dependency"
        )
    requirements = metadata.get_all("Requires-Dist") or []
    runtime_requirements = sorted(
        item for item in requirements if "extra ==" not in item
    )
    if runtime_requirements != EXPECTED_RUNTIME_DEPENDENCIES:
        raise UpdateError(
            "Spotter's runtime dependencies changed; update Formula resources"
        )


def update_formula(content: str, source: FormulaSource) -> tuple[str, bool]:
    match = SOURCE_PATTERN.search(content)
    if match is None:
        raise UpdateError(
            "Formula source URL/checksum contract was not found exactly once"
        )
    if SOURCE_PATTERN.search(content, match.end()) is not None:
        raise UpdateError("Formula contains more than one Spotter release source")

    current = FormulaSource(
        match.group("version"), match.group(0).split('"')[1], match.group("sha256")
    )
    if _version(source.version) < _version(current.version):
        raise UpdateError(
            f"refusing to downgrade Formula {current.version} to {source.version}"
        )
    if source.version == current.version:
        if source != current:
            raise UpdateError("published source changed without a version change")
        return content, False

    replacement = f'  url "{source.url}"\n  sha256 "{source.sha256}"'
    return content[: match.start()] + replacement + content[match.end() :], True


def _download(url: str, *, token: str | None = None) -> bytes:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "spotter-homebrew-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return cast(bytes, response.read())
    except (OSError, urllib.error.URLError) as error:
        raise UpdateError(f"could not download {url}: {error}") from error


def _load_json(url: str, *, token: str | None = None) -> Mapping[str, Any]:
    try:
        return _object(json.loads(_download(url, token=token)), "release API response")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpdateError(f"release API response is not valid JSON: {error}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formula", type=Path, default=Path("Formula/spotter.rb"))
    parser.add_argument("--release-api", default=RELEASE_API)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    try:
        release = _load_json(arguments.release_api, token=token)
        assets = _asset_map(release)
        tag = _text(release.get("tag_name"), "release tag")
        match = TAG_PATTERN.fullmatch(tag)
        if match is None:
            raise UpdateError(f"release tag {tag!r} is not vMAJOR.MINOR.PATCH")
        manifest_name = f"spotter-agent-{match.group('version')}-release.json"
        manifest_asset = assets.get(manifest_name)
        if manifest_asset is None:
            raise UpdateError(f"release is missing {manifest_name}")
        manifest_url = _text(
            manifest_asset.get("browser_download_url"), "manifest download URL"
        )
        manifest_content = _download(manifest_url, token=token)
        source = release_source(release, manifest_content)
        sdist_content = _download(source.url, token=token)
        verify_source_distribution(sdist_content, source)
        content = arguments.formula.read_text()
        updated, changed = update_formula(content, source)
        if changed:
            arguments.formula.write_text(updated)
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with Path(output).open("a") as destination:
                destination.write(f"changed={'true' if changed else 'false'}\n")
                destination.write(f"version={source.version}\n")
        print(f"spotter {source.version}: {'updated' if changed else 'current'}")
    except (OSError, UpdateError) as error:
        print(f"Formula update failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
