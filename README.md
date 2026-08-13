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
