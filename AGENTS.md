# a2a-hub

An **A2A store-and-forward hub**: a server that lets agents talk to each other over HTTPS
with bearer authentication, built on the official `a2a-sdk`.

## Goal and scope

**In scope:** the minimal layer the SDK does not provide — the executor (mailbox routing),
the task store, auth, the agent register, packaging.

**Out on purpose — and never counted as a gap:**

- **The A2A protocol is never reimplemented.** Everything protocol-shaped comes from
  `a2a-sdk` (Linux Foundation; HTTP server + SQL persistence included).
- **Deployment.** This repo publishes a container image; Kubernetes manifests and the deploy
  pipeline live in a **separate private infra repo** that consumes it.
- **Infra references.** This repo is publishable: GitHub/GHCR identifiers are fine, private
  cluster/domain/host details are not — those live in the infra repo.

## The A2A protocol (just enough to work here)

- Discovery: **Agent Card** at `/.well-known/agent-card.json` (capabilities + auth scheme).
- Transport: **JSON-RPC 2.0 over HTTPS**; the SDK handler requires the `A2A-Version: 1.0`
  header on every request, and the executor must enqueue a `Task` before any status update.
- Operations in use: `SendMessage`, `ListTasks`, `GetTask`. The **Task is persistent** —
  that is where the mailbox lives.
- Spec: https://a2a-protocol.org · SDK: https://github.com/a2aproject/a2a-python

## Architecture decisions

1. **Hub / store-and-forward, not peer-to-peer.** The clients are agent loops that *poll*
   each tick; they are not always-listening servers, so A2A's webhook push mode does not fit.
2. **One bearer token = one machine (the *principal*); the `A2A-Session` header is
   mandatory** and makes the identity `principal/session`, one mailbox per process. Identity
   always comes from the token and header, **never from the request body**: an agent
   describes itself, it does not choose who it is.
3. **Two mailboxes per agent, both addressable.** `principal/session` is one process's
   private mailbox. The bare `principal` is the agent-wide mailbox **and a broadcast**:
   every session of that agent reads it, and nothing in the message says it was a broadcast.
   Reserve it for "any session of that agent, I do not know which" — the store-and-forward
   case it exists for. Reads for `p/s` cover owners `{p/s, p}` (see `HubTaskStore`); the
   merged response is not paginated, agents poll with `status_timestamp_after`.
4. **Owner = recipient.** `HubAgentExecutor` reads `metadata.recipient` and stores the task
   under the recipient; on reads, `hub_owner_resolver` falls back to the authenticated agent,
   so **each agent sees only its own mailbox**. A missing or unknown recipient returns
   **HTTP 200 with a `REJECTED` task** whose reason lives only in `status.message` — a
   sender that does not read it cannot tell "refused" from "not yet read".
5. **Hub conventions are declared in the Agent Card as A2A extensions**, never as schema
   changes: session (`SESSION_EXTENSION_URI`, `required: true`), recipient
   (`RECIPIENT_EXTENSION_URI`, `required: true`), register (`required: false`).
6. **The register: agents declare what they are; the server says when it last saw them.**
   Declared fields (`role`, `host`, `projects`, `status`) are only as trustworthy as the
   caller. **`last_seen` is written by the server on every authenticated request** — a dead
   agent cannot claim to be alive, and it is the only field that answers "is this still
   true?". An agent that never introduced itself still appears as "seen, undeclared".
   Presence must never cost the mailbox anything: last-seen writes are throttled and
   register failures are swallowed, never propagated.
7. **SQLite first** (`A2A_HUB_DB_PATH`); PostgreSQL via `A2A_HUB_DB_URL` if volume grows.

**The wire contract is public API.** It has live clients: required headers, `recipient`
format, identities and method names change only through a PR, announced, preferring a
backwards-compatible transition over a hard cut-over. Changing storage semantics can strand
existing rows — check what is already in the task store before changing how owners resolve.

## Stack

- Python ≥3.12, managed with `uv`. Base package: `a2a-sdk[http-server,sqlite]`.
- Modules (`src/a2a_hub/`): `config.py` (settings from the environment), `auth.py`
  (`TokenRegistry`, constant-time comparison + ASGI bearer middleware; card and `/healthz`
  stay public), `executor.py` (mailbox + `hub_owner_resolver`), `store.py` (task store over
  SQLite/PostgreSQL), `registry.py` + `routes_registry.py` (the register), `marks.py` +
  `routes_marks.py` (read marks), `card.py` (Agent Card), `app.py` (Starlette factory),
  `server.py` (uvicorn startup), `client.py` (reference client: `HubClient` + `a2a-client`
  CLI, stdlib only, injectable transport).

| Command | What it is |
|---|---|
| `uv run a2a-hub` | the server (also `uv run python -m a2a_hub`) |
| `a2a-client` | reference poll-loop client; credentials from the environment or `~/.config/a2a-hub/agent.env` |
| `uv run pytest` | the test gate |
| `docker build -t a2a-hub .` | the image; run with `-e A2A_HUB_TOKENS="tok:agent"` |

Run locally: `A2A_HUB_TOKENS="tok:agent" uv run a2a-hub`. HTTPS: built-in TLS
(`A2A_HUB_TLS_CERTFILE`/`A2A_HUB_TLS_KEYFILE`) or any TLS-terminating proxy. Persistence:
mount a volume for the SQLite file, or point `A2A_HUB_DB_URL` at PostgreSQL.

## Security

- **Fail closed.** Tokens are required; `create_app` raises on empty tokens — the hub never
  comes up without auth. Tokens rotate by changing the secret and restarting, no rebuild.
- **Never commit secrets.** Tokens and `*.env` are git-ignored; new parameters are
  documented in `.env.example` with placeholders.
- **A secret never reaches a command line.** An argv is readable by every process on a
  shared host. Use `a2a-client`, which builds the `Authorization` header in-process; if a
  raw HTTP call is genuinely needed, pass the header by file (`curl --config`, written under
  `umask 077`), and never `set -x` in a script that handles a secret.
- **Hub-level mitigations live in the hub**, so a Docker-only deploy is protected too:
  request bodies are capped (`A2A_HUB_MAX_BODY_BYTES`, default 1 MiB → 413) and the bearer
  token is stripped from the call context (`RedactingContextBuilder`). Only pure transport
  concerns (TLS minimum version, HTTP→HTTPS redirect) belong to the infra layer.
- Never run with root `DEBUG` logging in production.

## Quality gates

CI is `.github/workflows/ci.yml`: the `test` job runs `uv lock --check` (a stale lockfile is
a cache nothing else checks) and `uv run pytest` with **coverage ≥ 90%**, enforced by
`--cov-fail-under=90` in `pyproject.toml`.

- **Every feature has a functional test** that exercises the real flow over the A2A protocol
  (JSON-RPC against the ASGI app with `httpx`); unit tests cover branches hard to force over
  HTTP. Coverage is a floor, not the goal: a test must encode the real use case — the
  session change once shipped at 100% coverage and still broke store-and-forward.
- **Green tests do not prove the image builds.** `pytest` never containerizes. Run
  `docker build .` before trusting a green check on anything touching `Dockerfile`,
  `pyproject.toml` or the packaged files.

## Delivery: what is structural in this repo

- **The image is built once, in the PR job, keyed by the git tree** (the content, not the
  commit). On a push to `main`, `publish` **does not build — it promotes**
  `ghcr.io/tlmak0/a2a-hub:tree-$(git rev-parse HEAD^{tree})` and **fails on purpose** when
  that tag does not exist. So `main` only receives, by fast-forward, heads that a PR
  actually built — a direct push, even documentation, leaves `publish` red with nothing to
  promote.
- **A merge to `main` restarts the hub** (one replica, `Recreate`, RWO volume): the infra
  repo's scheduled bump deploys the newly promoted digest, and the whole fleet is blind for
  a few seconds. Merges therefore ride **announced windows, in groups** — the window, stack
  and hold procedure is in the skill `delivering-a-branch`.
- **The client is RELEASED, not deployed.** The owner's decision, in his words: *"¿qué tiene
  que ver el cliente con el servidor, por qué se tiene que desplegar el cliente? Si acaso
  compilarlo como mucho y crear una release"*. `git tag v<version> && git push origin
  v<version>` runs `release.yml`: the gate, then the build script, then a GitHub Release
  carrying the wheel — and the release script installs that wheel into a clean venv and
  **runs** `a2a-client`, because only running the artifact catches a dropped console script.
  A tag builds no image and restarts nothing; hosts install a **version**.

## Conventions

- **Everything that lives in the repo is in English** — code, comments, commits, issues,
  PRs, docs. Spanish is for what does *not* live here: hub messages between agents, and
  anything written to the owner. His decision, and **it is not retroactive**: nothing old
  gets translated, because rewriting a finished conversation breaks its links without fixing
  a defect.
- Commits with a scope prefix (`feat(server):`, `fix(auth):`, `docs:`), in English.

## How work happens here

**State does not live in this file: it lives in GitHub.** Open issues are the state, closed
issues are the history. This document holds only what will still be true tomorrow without
anyone editing it.

Load these skills depending on what you are about to do:

| You are about to… | Skill |
|---|---|
| decide where to record something, open an issue, pick up work after a restart | `github-is-the-state` |
| turn an idea or bug into work, draft an acceptance criterion | `designing-into-issues` |
| run or interpret a gate, or claim something passes | `quality-gates` |
| write tests or fixtures | `bdd-integration-first` |
| write an assertion, or interpret a green, a zero or a "does not reproduce" | `rules-of-evidence` |
| open a PR, merge, promote an image, plan a window | `delivering-a-branch` |
| review security, handle a secret, respond to a suspected compromise | `security-review-and-incidents` |
| write user-visible strings or render a failure | `ui-texts-and-failures` |

The history of how each rule here was earned — the token leaks, the broadcast that read as
four private orders, the runbook a shell executed — is in this file's git history
(`git log -p AGENTS.md`; it had 701 lines). You do not need it to work.
