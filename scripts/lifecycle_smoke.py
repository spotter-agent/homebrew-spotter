#!/usr/bin/env python3
"""Exercise the packaged install, live upgrade, uninstall, and reinstall lifecycle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

G1 = "9.98.0"
G2 = "9.99.0"
FIXTURE_TAP = "spotter-lifecycle/fixture"
FIXTURE_FORMULA = "spotter-lifecycle"
QUALIFIED_FORMULA = f"{FIXTURE_TAP}/{FIXTURE_FORMULA}"
SERVICE_LABEL = "dev.spotter.runtime"
SOURCE_BLOCK = re.compile(
    r'^  url "[^"]+spotter_agent-[^"]+\.tar\.gz"\n'
    r'(?:  version "[^"]+"\n)?'
    r'  sha256 "[0-9a-f]{64}"$',
    re.MULTILINE,
)


class LifecycleSmokeError(RuntimeError):
    """The fixture found a lifecycle contract violation."""


@dataclass(frozen=True)
class RuntimeStatus:
    pid: int
    version: str
    build_id: str


def _run(
    arguments: Sequence[str | Path],
    *,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = [str(argument) for argument in arguments]
    result = subprocess.run(
        command,
        env=dict(env) if env is not None else None,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LifecycleSmokeError(
            f"{' '.join(command)} failed ({result.returncode}): {detail}"
        )
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_fingerprint(paths: Sequence[Path]) -> dict[str, str]:
    return {str(path): _sha256(path) for path in paths}


def _copy_source(source: Path, destination: Path) -> None:
    listed = _run(
        [
            "git",
            "-C",
            source,
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ]
    ).stdout
    for relative_text in listed.split("\0"):
        if not relative_text:
            continue
        relative = Path(relative_text)
        incoming = source / relative
        outgoing = destination / relative
        outgoing.parent.mkdir(parents=True, exist_ok=True)
        if incoming.is_symlink():
            outgoing.symlink_to(os.readlink(incoming))
        elif incoming.is_file():
            shutil.copy2(incoming, outgoing)


def _fixture_source(source: Path, destination: Path) -> None:
    destination.mkdir()
    _copy_source(source, destination)
    _run(["git", "-C", destination, "init", "--initial-branch=main"])
    _run(["git", "-C", destination, "config", "user.name", "Spotter lifecycle fixture"])
    _run(["git", "-C", destination, "config", "user.email", "fixture@spotter.invalid"])
    _run(["git", "-C", destination, "add", "."])
    commit_env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    _run(
        ["git", "-C", destination, "commit", "-m", "Fixture generation one"],
        env=commit_env,
    )
    _run(["git", "-C", destination, "tag", f"v{G1}"])
    second_env = {
        **commit_env,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:01Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:01Z",
    }
    _run(
        [
            "git",
            "-C",
            destination,
            "commit",
            "--allow-empty",
            "-m",
            "Fixture generation two",
        ],
        env=second_env,
    )
    _run(["git", "-C", destination, "tag", f"v{G2}"])


def _build_generation(
    source: Path, repository: Path, output: Path, version: str
) -> Path:
    _run(
        [
            sys.executable,
            source / "scripts/build_release.py",
            "--repo",
            repository,
            "--tag",
            f"v{version}",
            "--output-dir",
            output,
        ]
    )
    artifact = output / f"spotter_agent-{version}.tar.gz"
    if not artifact.is_file():
        raise LifecycleSmokeError(f"fixture sdist was not built: {artifact}")
    return artifact


def _formula_text(template: str, artifact: Path, version: str) -> str:
    updated = template.replace(
        "class Spotter < Formula", "class SpotterLifecycle < Formula", 1
    )
    replacement = (
        f'  url "{artifact.resolve().as_uri()}"\n'
        f'  version "{version}"\n'
        f'  sha256 "{_sha256(artifact)}"'
    )
    updated, replacements = SOURCE_BLOCK.subn(replacement, updated, count=1)
    if replacements != 1:
        raise LifecycleSmokeError(
            "could not identify the Formula source URL/checksum block"
        )
    return updated


def _write_formula(path: Path, template: str, artifact: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_formula_text(template, artifact, version))


def _fake_codex(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import signal\n"
        "import sys\n"
        "\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('codex-cli 0.147.0')\n"
        "elif args == ['--help']:\n"
        "    print('--remote')\n"
        "elif args == ['app-server', '--help']:\n"
        "    print('--listen')\n"
        "elif args == ['app-server']:\n"
        "    while True:\n"
        "        signal.pause()\n"
    )
    path.chmod(0o755)


def _runtime_status(spotter: Path, env: Mapping[str, str]) -> RuntimeStatus:
    output = _run([spotter, "daemon", "status"], env=env).stdout.strip()
    match = re.search(
        r"healthy \(pid=(?P<pid>\d+), protocol=\d+, version=(?P<version>[^,]+), "
        r"build=(?P<build>[^)]+)\)",
        output,
    )
    if match is None:
        raise LifecycleSmokeError(f"unexpected daemon status: {output}")
    return RuntimeStatus(int(match["pid"]), match["version"], match["build"])


def _wait_for_status(
    spotter: Path, env: Mapping[str, str], *, timeout: float = 15.0
) -> RuntimeStatus:
    deadline = time.monotonic() + timeout
    detail = "daemon unavailable"
    while time.monotonic() < deadline:
        result = _run([spotter, "daemon", "status"], env=env, check=False)
        if result.returncode == 0:
            return _runtime_status(spotter, env)
        detail = (result.stderr or result.stdout).strip() or detail
        time.sleep(0.2)
    raise LifecycleSmokeError(detail)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_exit(pid: int, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return
        time.sleep(0.2)
    raise LifecycleSmokeError(f"daemon pid {pid} survived package uninstall")


def _terminate_fixture_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _launchd_state(label: str) -> str:
    result = _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], check=False)
    return result.stdout if result.returncode == 0 else ""


def _hook_command(codex_home: Path) -> str:
    raw = json.loads((codex_home / "hooks.json").read_text())
    for group in raw["hooks"]["SessionStart"]:
        for hook in group["hooks"]:
            command = str(hook.get("command", ""))
            if "spotter hook" in command:
                return command
    raise LifecycleSmokeError("SessionStart has no Spotter-owned Hook command")


def _hook_count(codex_home: Path) -> int:
    raw = json.loads((codex_home / "hooks.json").read_text())
    return sum(
        1
        for groups in raw["hooks"].values()
        for group in groups
        for hook in group["hooks"]
        if "spotter hook" in str(hook.get("command", ""))
    )


def _invoke_cached_hook(
    command: str, env: Mapping[str, str], session: str
) -> tuple[int, str]:
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": session})
    result = _run(["/bin/sh", "-c", command], env=env, input_text=payload, check=False)
    return result.returncode, result.stderr


def _assert(condition: bool, detail: str) -> None:
    if not condition:
        raise LifecycleSmokeError(detail)


def _cleanup(
    brew: Path,
    formula: str,
    service_registration: Path,
    env: Mapping[str, str],
) -> None:
    spotter = Path(_run([brew, "--prefix"], check=False).stdout.strip()) / "bin/spotter"
    if spotter.exists():
        _run([spotter, "teardown", "codex"], env=env, check=False)
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{SERVICE_LABEL}"], check=False)
    if service_registration.exists():
        service_registration.unlink()
    _run([brew, "services", "stop", formula], check=False)
    if _run([brew, "list", "--versions", FIXTURE_FORMULA], check=False).stdout.strip():
        _run([brew, "uninstall", "--force", formula], check=False)
    _run([brew, "untrust", "--formula", formula], check=False)
    _run([brew, "untap", FIXTURE_TAP], check=False)


def lifecycle_smoke(spotter_source: Path, formula_template: Path) -> None:
    brew_text = shutil.which("brew")
    if sys.platform != "darwin" or brew_text is None:
        raise LifecycleSmokeError(
            "the Homebrew lifecycle fixture requires macOS and brew"
        )
    brew = Path(brew_text)
    prefix = Path(_run([brew, "--prefix"]).stdout.strip())
    stable_cli = prefix / "bin/spotter"
    stable_daemon = prefix / "bin/spotterd"
    registration = Path.home() / "Library/LaunchAgents" / f"{SERVICE_LABEL}.plist"
    _assert(
        not stable_cli.exists() and not stable_daemon.exists(),
        "Spotter is already installed",
    )
    _assert(not registration.exists(), f"refusing to replace existing {registration}")
    _assert(not _launchd_state(SERVICE_LABEL), f"{SERVICE_LABEL} is already loaded")

    with tempfile.TemporaryDirectory(prefix="spotter-homebrew-lifecycle-") as temporary:
        root = Path(temporary)
        fixture_repository = root / "source"
        artifacts = root / "artifacts"
        codex_home = root / "codex"
        spotter_home = root / "spotter"
        fake_bin = root / "bin"
        codex_home.mkdir()
        spotter_home.mkdir()
        user_config = spotter_home / "spotter.toml"
        user_config.write_text('[main_agent]\nadapter = "codex"\n')
        _fake_codex(fake_bin / "codex")
        config = codex_home / "config.toml"
        hooks = codex_home / "hooks.json"
        config.write_text("[features]\nuser_owned = true\n")
        hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionStart": [
                            {"hooks": [{"type": "command", "command": "notify-user"}]}
                        ]
                    }
                },
                indent=2,
            )
            + "\n"
        )
        codex_before = _tree_fingerprint([config, hooks])
        env = {
            **os.environ,
            "CODEX_HOME": str(codex_home),
            "SPOTTER_HOME": str(spotter_home),
            "PATH": f"{fake_bin}:{prefix / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOMEBREW_NO_AUTO_UPDATE": "1",
        }
        shared_app_server = subprocess.Popen(
            [fake_bin / "codex", "app-server"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            _assert(
                _process_exists(shared_app_server.pid),
                "shared App Server fixture did not start",
            )
            print("[fixture] build two immutable Spotter generations", flush=True)
            _fixture_source(spotter_source, fixture_repository)
            g1 = _build_generation(
                spotter_source, fixture_repository, artifacts / "g1", G1
            )
            g2 = _build_generation(
                spotter_source, fixture_repository, artifacts / "g2", G2
            )

            print("[fixture] create an isolated local tap and install G1", flush=True)
            _run([brew, "tap-new", "--no-git", FIXTURE_TAP], env=env)
            tap = Path(
                _run([brew, "--repository", FIXTURE_TAP], env=env).stdout.strip()
            )
            formula = tap / "Formula" / f"{FIXTURE_FORMULA}.rb"
            template = formula_template.read_text()
            _write_formula(formula, template, g1, G1)
            _run([brew, "trust", "--formula", QUALIFIED_FORMULA], env=env)
            _run([brew, "install", "--build-from-source", QUALIFIED_FORMULA], env=env)
            _assert(
                _tree_fingerprint([config, hooks]) == codex_before,
                "brew install edited Codex",
            )
            _assert(
                f"spotter {G1}" in _run([stable_cli, "--version"], env=env).stdout,
                "bad G1 CLI",
            )
            _assert(
                f"spotterd {G1}" in _run([stable_daemon, "--version"], env=env).stdout,
                "bad G1 daemon",
            )

            print(
                "[fixture] configure Codex, verify the bridge, and keep G1 live",
                flush=True,
            )
            _run([stable_cli, "setup", "codex"], env=env)
            g1_runtime = _wait_for_status(stable_cli, env)
            _assert(g1_runtime.version == G1, "setup did not start G1")
            _assert(
                _process_exists(shared_app_server.pid),
                "setup terminated the shared App Server",
            )
            manifest_path = spotter_home / "integrations/codex.json"
            manifest_g1 = json.loads(manifest_path.read_text())
            serialized_g1 = json.dumps(manifest_g1)
            _assert(
                "Cellar" not in serialized_g1, "G1 manifest persisted a Cellar path"
            )
            _assert(
                _hook_count(codex_home) == 4, "setup did not create one Hook generation"
            )
            _run([stable_cli, "setup", "codex"], env=env)
            idempotent_g1 = _runtime_status(stable_cli, env)
            idempotent_manifest = json.loads(manifest_path.read_text())
            _assert(
                idempotent_g1 == g1_runtime,
                "idempotent setup replaced the healthy G1 daemon",
            )
            _assert(
                idempotent_manifest["integration_generation"]
                == manifest_g1["integration_generation"],
                "idempotent setup rotated the active generation",
            )
            _assert(
                _hook_count(codex_home) == 4,
                "idempotent setup duplicated Hook registration",
            )
            cached_g1_hook = _hook_command(codex_home)
            code, stderr = _invoke_cached_hook(cached_g1_hook, env, "retained")
            _assert(code == 0 and not stderr, "packaged G1 Hook bridge failed")
            journal = spotter_home / "sessions/retained.jsonl"
            labels = spotter_home / "labels/retained.json"
            experiment = spotter_home / "experiments/retained.json"
            registry = spotter_home / "repos.json"
            labels.parent.mkdir()
            experiment.parent.mkdir()
            labels.write_text('{"retained":true}\n')
            experiment.write_text('{"retained":true}\n')
            registry.write_text('{"repositories":[]}\n')
            durable = [journal, labels, experiment, registry, user_config]
            durable_before = _tree_fingerprint(durable)

            print(
                "[fixture] upgrade to G2 without stopping the live G1 daemon",
                flush=True,
            )
            _write_formula(formula, template, g2, G2)
            _run([brew, "upgrade", "--build-from-source", QUALIFIED_FORMULA], env=env)
            _assert(
                _process_exists(g1_runtime.pid),
                "Homebrew upgrade stopped G1 before reconcile",
            )
            _assert(
                f"spotter {G2}" in _run([stable_cli, "--version"], env=env).stdout,
                "bad G2 CLI",
            )
            still_g1 = _runtime_status(stable_cli, env)
            _assert(
                still_g1.pid == g1_runtime.pid,
                f"G1 daemon was replaced during upgrade: {g1_runtime} -> {still_g1}",
            )
            _assert(
                still_g1.build_id == g1_runtime.build_id,
                f"G1 daemon masqueraded as G2: {g1_runtime} -> {still_g1}",
            )
            journal_size = journal.stat().st_size
            code, stderr = _invoke_cached_hook(
                cached_g1_hook, env, "mixed-before-reconcile"
            )
            _assert(code == 0, "mixed-generation Hook did not fail open")
            _assert(
                "package build is stale" in stderr, "mixed generation was not diagnosed"
            )
            _assert(
                journal.stat().st_size == journal_size, "G2 accepted the cached G1 Hook"
            )

            print("[fixture] reconcile exactly once onto G2", flush=True)
            _run([stable_cli, "setup", "codex"], env=env)
            g2_runtime = _wait_for_status(stable_cli, env)
            _assert(g2_runtime.version == G2, "reconcile did not start G2")
            _assert(g2_runtime.pid != g1_runtime.pid, "reconcile did not replace G1")
            _assert(not _process_exists(g1_runtime.pid), "G1 survived reconciliation")
            _assert(
                _process_exists(shared_app_server.pid),
                "upgrade reconciliation terminated the shared App Server",
            )
            manifest_g2 = json.loads(manifest_path.read_text())
            _assert(
                manifest_g2["integration_generation"]
                != manifest_g1["integration_generation"],
                "integration generation did not rotate",
            )
            _assert(
                "Cellar" not in json.dumps(manifest_g2),
                "G2 manifest persisted a Cellar path",
            )
            _assert(
                "Cellar" not in registration.read_text(),
                "service persisted a Cellar path",
            )
            _assert(
                _hook_count(codex_home) == 4, "duplicate Hook generations became active"
            )
            code, stderr = _invoke_cached_hook(
                cached_g1_hook, env, "mixed-after-reconcile"
            )
            _assert(
                code == 0 and "stale integration generation" in stderr,
                "G1 Hook was not fenced",
            )
            _assert(
                _tree_fingerprint(durable) == durable_before,
                "upgrade changed durable user data",
            )

            print(
                "[fixture] uninstall without teardown and verify bounded fail-open state",
                flush=True,
            )
            cached_g2_hook = _hook_command(codex_home)
            _run([brew, "uninstall", QUALIFIED_FORMULA], env=env)
            _assert(
                not stable_cli.exists() and not stable_daemon.exists(),
                "executables remain",
            )
            _wait_for_exit(g2_runtime.pid)
            first_state = _launchd_state(SERVICE_LABEL)
            time.sleep(2.5)
            second_state = _launchd_state(SERVICE_LABEL)
            _assert(
                "pid =" not in second_state, "launchd kept the removed daemon alive"
            )
            first_runs = re.search(r"runs = (\d+)", first_state)
            second_runs = re.search(r"runs = (\d+)", second_state)
            if first_runs is not None and second_runs is not None:
                _assert(
                    first_runs[1] == second_runs[1],
                    "launchd retried a removed executable",
                )
            code, _ = _invoke_cached_hook(cached_g2_hook, env, "after-uninstall")
            _assert(code == 0, "dangling integration did not fail open")
            _assert(
                _tree_fingerprint(durable) == durable_before,
                "uninstall purged user data",
            )
            _assert(
                _process_exists(shared_app_server.pid),
                "teardown-less uninstall terminated the shared App Server",
            )

            print(
                "[fixture] reinstall, repair idempotently, teardown, and uninstall cleanly",
                flush=True,
            )
            _run([brew, "install", "--build-from-source", QUALIFIED_FORMULA], env=env)
            status = _run([stable_cli, "status"], env=env, check=False)
            doctor = _run([stable_cli, "doctor"], env=env, check=False)
            diagnostic = status.stdout + status.stderr + doctor.stdout + doctor.stderr
            _assert(
                "Codex integration: ready (managed)" in diagnostic,
                "reinstall did not identify the retained managed integration",
            )
            _run([stable_cli, "setup", "codex"], env=env)
            repaired = _wait_for_status(stable_cli, env)
            _assert(repaired.version == G2, "reinstall did not restore G2")
            _assert(_hook_count(codex_home) == 4, "repair duplicated Hook registration")
            _run([stable_cli, "teardown", "codex"], env=env)
            _assert(not registration.exists(), "teardown left its service registration")
            _assert(
                "notify-user" in hooks.read_text(),
                "teardown changed unrelated Codex Hooks",
            )
            _assert(
                config.read_text() == "[features]\nuser_owned = true\n",
                "teardown edited config",
            )
            _run([brew, "uninstall", QUALIFIED_FORMULA], env=env)
            _wait_for_exit(repaired.pid)
            _assert(
                _tree_fingerprint(durable) == durable_before,
                "clean removal purged user data",
            )
            _assert(
                _process_exists(shared_app_server.pid),
                "teardown and uninstall terminated the shared App Server",
            )
        finally:
            _cleanup(brew, QUALIFIED_FORMULA, registration, env)
            _terminate_fixture_process(shared_app_server)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spotter-source", type=Path, required=True)
    parser.add_argument(
        "--formula-template", type=Path, default=Path("Formula/spotter.rb")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        lifecycle_smoke(
            arguments.spotter_source.resolve(), arguments.formula_template.resolve()
        )
    except (OSError, ValueError, LifecycleSmokeError) as error:
        print(f"Homebrew lifecycle smoke failed: {error}", file=sys.stderr)
        return 1
    print("Homebrew lifecycle smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
