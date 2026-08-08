# CLAUDE.md

Guide for Claude Code when working on **a2a-hub**. For the full context read `README.md`
(what it is and status) and `AGENTS.md` (architecture, security and deployment decisions).

## What it is

A2A server (Agent2Agent protocol) so agents can communicate over HTTPS with authentication.
A minimal, **self-contained** layer (`AgentExecutor` + task store + auth) on top of the
official **`a2a-sdk`**. The protocol is not reimplemented.

## Layout

- `src/a2a_hub/` — server app and `AgentExecutor` (config, auth, executor, store, card, app,
  server) plus `client.py` (reference client for the agent loop: `a2a-client` CLI).
- `Dockerfile` — self-contained image (token auth in-process, optional built-in TLS).
- `tests/` — functional + unit tests (`SendMessage` → `ListTasks` flow, auth, etc.).
- `.env.example` — parameters with placeholders (never real values).

## Commands

- Environment: `uv sync` (creates `.venv` with runtime + dev deps).
- Run locally: `A2A_HUB_TOKENS="tok:agent" uv run a2a-hub` (config via the environment, see
  `.env.example`). Also `uv run python -m a2a_hub`.
- Tests: `uv run pytest` (fails if coverage drops below 90%).
- Image: `docker build -t a2a-hub .`

## Non-negotiable rules

- **Everything in English** — code, comments, docstrings, docs and commit messages.
- **No infra references in this repo** — it is publishable. GitHub/GHCR identifiers
  (`ghcr.io/tlmak0/...`) and authorship are fine; private cluster/domain/host details are not.
  Those live in the separate private infra repo.
- **Never commit secrets** (tokens, `.env`). They go in `.gitignore` and in a secret store.
- **Do not expose without auth or without a security review.** Auth is in-process; serve HTTPS
  via built-in TLS (`A2A_HUB_TLS_*`) or a proxy.
- Fail closed: tokens are required; the process must crash if they are missing (no tokens =
  no startup, never silently exposed).
- Run the tests before containerizing or publishing.
- **Every feature has functional tests and global coverage stays ≥ 90%**
  (`--cov-fail-under=90` in `pyproject.toml`). Adding a feature = adding its test that
  exercises the real flow over the A2A protocol, not just the isolated module.
- Project knowledge lives in these docs (`README.md`, `AGENTS.md`, `CLAUDE.md`), not in a
  separate memory store.

## Deployment

This repo is the **self-contained service** only. Run it anywhere with Docker + tokens
(`docker run -e A2A_HUB_TOKENS=... a2a-hub`), optionally with built-in TLS. CI
(`.github/workflows/ci.yml`) runs the test gate and publishes the image to GHCR
(`ghcr.io/tlmak0/a2a-hub`). Kubernetes manifests and the deploy pipeline live in a
**separate private infra repo** that consumes the GHCR image.
