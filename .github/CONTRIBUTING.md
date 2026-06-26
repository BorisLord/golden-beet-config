# Contributing

Thanks for your interest. This is a small, focused project — bug reports, fixes, and well-scoped features are
all welcome.

## Workflow

External contributions go through **fork + Pull Request**:

1. Fork the repo and create a branch (`git checkout -b fix/short-description`).
2. Make your change, keep it focused (one concern per PR).
3. Ensure tests and lint pass (below), then open a PR against `master` describing *why*, not just *what*.

## Dev setup

Tooling is pinned with [mise](https://mise.jdx.dev):

```bash
mise install        # python 3.13 + uv + ruff
./setup.sh          # installs beets (+ plugins) and the gbc CLI; prints any missing system tools
```

System binaries beets needs (the setup script checks and prints the install line): `fpcalc` (chromaprint),
`ffmpeg`, `flac`, `mp3val`.

API keys are redacted to `REPLACE_ME` in the committed config — supply your own. Runtime paths live in
`config.env` (copy `config.env.example`, it's gitignored).

## Before you push

```bash
mise run lint       # ruff
mise run test       # stdlib unittest — NO pytest, NO network in tests
```

Both run in CI on every PR (`.github/workflows/test.yml`); a green local run should mean a green CI.

Tests use `unittest` only and must not hit the network — mock at the boundary (HTTP/MusicBrainz, time). Real
filesystem/db work uses the in-repo test base.

## Conventions

- Comment the *non-obvious* (why), not the what. Reference `file:line` rather than pasting code.
- Read **[AGENTS.md](../AGENTS.md)** first — it's the operational brief, including the **CRITICAL RULES** learned
  the hard way (never delete → move to the dump dir; never bulk-`modify`/`move` without a query; test on ~10
  before the whole library). Destructive shortcuts will be rejected in review.

By contributing you agree your work is licensed under the project's [MIT License](../LICENSE).
