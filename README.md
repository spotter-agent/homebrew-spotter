# Homebrew tap for Spotter

This is the official Homebrew tap for
[Spotter](https://github.com/spotter-agent/spotter), a runtime trajectory supervisor for coding
agents.

## Install

Install the standalone CLI, Hook bridge, and daemon from the latest qualified release:

```bash
brew install spotter-agent/spotter/spotter
```

The package install does not edit Codex configuration. Connect Spotter to Codex explicitly after
installation:

```bash
spotter setup codex
```

The Formula exposes both `spotter` and `spotterd`. To operate the package-provided background
service directly:

```bash
brew services start spotter-agent/spotter/spotter
brew services stop spotter-agent/spotter/spotter
```

Spotter keeps configuration, journals, integration ownership, runtime state, and logs outside its
versioned Homebrew keg under `~/.spotter` by default. Removing the Formula does not remove that user
data or edit Codex configuration.

## Release updates

`Formula/spotter.rb` consumes the immutable source distribution attached to a published
`vMAJOR.MINOR.PATCH` release in `spotter-agent/spotter`. The Formula records the release asset's
SHA-256 and pins every Python runtime dependency as a separate resource.

The hourly and manually dispatchable **Update Spotter Formula** workflow reads Spotter's published
release manifest, validates its tag, artifact name, size, and digest against the GitHub Release, and
opens a version-specific pull request when a newer release exists. It refuses drafts, prereleases,
downgrades, same-version digest changes, and malformed release manifests.

To reproduce an update locally:

```bash
python3 scripts/update_formula.py
brew update-python-resources spotter-agent/spotter/spotter \
  --package-name websockets --version 17.0 --print-only
brew audit --strict --online spotter-agent/spotter/spotter
brew test spotter-agent/spotter/spotter
```

If Spotter's runtime dependencies change, update and review the Formula resources in the same pull
request. The automated source URL/checksum update intentionally does not guess a new dependency
contract.

## Lifecycle smoke

The macOS lifecycle job builds two immutable local Spotter generations from the supplied source,
publishes them through an isolated fixture tap, and exercises this path without relying on an old
Cellar keg remaining present:

```text
install G1 → setup Codex → keep G1 daemon live → upgrade to G2
  → diagnose G1 → reconcile once onto G2 → uninstall without teardown
  → verify fail-open + retained data → reinstall → teardown → uninstall
```

It also checks that Codex files are unchanged by package installation, persisted integration/service
paths contain no `Cellar` component, cached G1 Hooks cannot attach as G2, a removed executable does
not enter a service retry loop, unrelated Codex Hooks survive teardown, and user config/journal/
label/experiment/registry fixtures survive both uninstall routes.

To reproduce the same fixture from sibling tap and Spotter checkouts on macOS:

```bash
python3 -m venv /tmp/spotter-lifecycle-venv
/tmp/spotter-lifecycle-venv/bin/python -m pip install 'build>=1.2,<2'
/tmp/spotter-lifecycle-venv/bin/python scripts/lifecycle_smoke.py \
  --spotter-source ../spotter \
  --formula-template Formula/spotter.rb
```

The harness refuses to replace an installed `spotter` executable or existing
`dev.spotter.runtime` LaunchAgent and cleans up its isolated Formula, service, trust entry, and tap
on success or failure.

For coordinated cross-repository pull requests, CI resolves a same-named branch in
`spotter-agent/spotter` to an exact commit before falling back to Spotter `main`. This lets the tap
exercise runtime changes before either companion pull request merges.
