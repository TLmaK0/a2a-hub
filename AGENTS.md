# AGENTS Instructions — a2a-hub

Procedure and knowledge for working in this repo. The *what it is* and status live in
`README.md`; here goes the *how* and the *why* of the decisions.

## Goal

An A2A server that lets agents talk over HTTPS with authentication, reusing the standard
protocol and the official SDK. **We do not reimplement the A2A protocol:**
everything protocol-related comes from `a2a-sdk`; this repo is only the minimal layer
(executor, task store, auth, packaging and deployment).

## The A2A protocol (just enough to work here)

- Discovery: **Agent Card** at `/.well-known/agent-card.json` (capabilities + auth scheme).
- Transport: **JSON-RPC 2.0 over HTTPS** (primary). SSE for streaming.
- Operations we use: `SendMessage`, `ListTasks`, `GetTask`. (Streaming/push, later.)
- Data model: `Task`, `Message`, `Part`, `Artifact`. The **Task is persistent** — that is
  where the mailbox lives.
- Spec: https://a2a-protocol.org · SDK: https://github.com/a2aproject/a2a-python

## Architecture decisions (and why)

1. **Hub / store-and-forward, not peer-to-peer.** The agents are Claude Code loops that
   *poll* on each tick; they are not always-listening servers. A central server that
   persists tasks + `ListTasks` per tick solves this **within** the A2A standard (A2A's
   "pure" async mode is webhook push-notifications, which require a listening receiver —
   does not fit).
2. **Official `a2a-sdk`, not `a2a-server`.** The `Agent-2-Agent/a2a-server` repo is dead and
   does not implement the protocol. `a2a-sdk` is alive (Linux Foundation, ~2k★) and ships an
   HTTP server + SQL persistence.
3. **Bearer token auth per agent, identity `principal/session`.** One token = one machine
   (the *principal*, for per-identity rate-limit/audit). On top of that every client
   **must** send the `A2A-Session` header: the identity becomes `principal/session` and
   each process gets its own mailbox.
   - *Why mandatory:* without it, two processes holding the same token would silently
     share a mailbox and both would process the same message (there is no ack/consume).
   - The principal always comes from the token, so a session can never impersonate
     another machine; sessions of one machine are mutually trusted (same token).
   - **Two mailboxes per agent, both addressable.** `principal` is the agent-wide
     mailbox, read by *every* session of that agent; `principal/session` is one
     process's private mailbox. `ListTasks`/`GetTask` for `p/s` therefore read owners
     `{p/s, p}` (see `HubTaskStore`).
   - *Why:* store-and-forward means leaving a message for an agent that is **not
     awake**, whose session name you cannot possibly know. An earlier iteration made a
     bare principal non-addressable ("nobody could read it") and that broke the hub's
     whole purpose — sending to `agent-b` returned `REJECTED`, and messages already
     stored under a bare owner became unreadable. Sessions must isolate *processes*,
     never make an agent unreachable. Addressing is session-optional; authenticating
     is not.
   - Caveat: when the two mailboxes are merged, the response is not paginated
     (`page_size` still caps the batch); agents poll with `status_timestamp_after`.
   mTLS is a future improvement if more strength is needed.
4. **SQLite first.** A single file is enough to start; migrate to PostgreSQL if volume grows.
5. **Per-recipient routing (owner = recipient).** `DatabaseTaskStore` scopes each `Task` to an
   *owner*. In a mailbox the owner must be **who receives**, not who sends:
   - On `SendMessage`, `HubAgentExecutor` reads `metadata.recipient`, sets
     `owner_override = recipient` in `ServerCallContext.state` **before** emitting events, and
     the store saves the task under the recipient.
   - On `ListTasks`/`GetTask` there is no executor: `hub_owner_resolver` falls back to the
     authenticated agent, so **each agent only sees its own mailbox** (isolation covered by
     tests). The message travels as an *artifact* with `sender`/`recipient` metadata.
   - Missing or unknown recipient ⇒ `REJECTED` task (visible only to the sender).

## Security — hard rules

- **Never commit secrets.** Tokens, credentials and `*.env` go in `.gitignore` and, in
  production, in a secret store (env vars / k8s Secrets). If a new parameter is needed,
  document it in `.env.example` with a placeholder.
- **Do not expose without auth.** Auth is enforced in-process (bearer token), so the service
  is safe to run standalone. Public exposure should still follow a security review (SDK,
  dependencies, TLS). Serve HTTPS either via built-in TLS (`A2A_HUB_TLS_*`) or a TLS proxy.
- Fail closed: tokens are **required**; without them the process refuses to start
  (`create_app` raises on empty tokens). The hub never comes up without auth.
- Rotation: tokens must be rotatable by changing the config/secret and restarting, without
  rebuilding the image.
- **Hub-level mitigations live in the hub** (so a Docker-only deploy is protected too), not
  in the proxy: request bodies are capped (`A2A_HUB_MAX_BODY_BYTES`, default 1 MiB → 413),
  and the bearer token is stripped from the call context (`RedactingContextBuilder`) so it
  cannot leak via a state/context dump. Only pure transport concerns (TLS minimum version,
  HTTP→HTTPS redirect when a proxy terminates TLS) belong to the infra layer.
- Never run with root `DEBUG` logging in production (default `info` is fine).

## Development

- Python 3.12. Env/deps manager: `uv` (or `pip` + venv). Base package:
  `a2a-sdk[http-server,sqlite]` (SDK 1.x, protobuf types, A2A protocol `1.0`).
- Run locally: `A2A_HUB_TOKENS="tok:agent" uv run a2a-hub`.
- Modules (`src/a2a_hub/`):
  - `config.py` — `Settings` from the environment.
  - `auth.py` — `TokenRegistry` (rotatable, constant-time comparison) + ASGI bearer
    middleware (401; leaves the card and `/healthz` public).
  - `executor.py` — `HubAgentExecutor` (mailbox) + `hub_owner_resolver`.
  - `store.py` — `DatabaseTaskStore` over SQLite/PostgreSQL.
  - `card.py` — Agent Card. `app.py` — Starlette factory. `server.py` — uvicorn startup.
  - `client.py` — reference client for the agent poll loop (`HubClient` + `a2a-client`
    CLI). Stdlib only; credentials from the environment or `~/.config/a2a-hub/agent.env`.
    Its transport is injectable, so tests drive the real client against the real app.
- Protocol note: the SDK handler requires the `A2A-Version: 1.0` header on every JSON-RPC
  request and that the executor **enqueue a `Task` before** any status update.

## Testing (hard rule)

- **Global coverage ≥ 90%**, enforced by `--cov-fail-under=90` in `pyproject.toml` (currently
  100%). A PR that drops it below 90% fails and is not merged.
- **Every feature has a functional test**: it exercises the real flow over the A2A protocol
  (JSON-RPC against the ASGI app with `httpx`), not just the isolated module. Unit tests
  complement branches that are hard to force over HTTP (cancel, rejections, resolver).
- Run: `uv run pytest`. Run the tests **before** containerizing or publishing.
- test→feature map: `test_auth_http`/`test_auth_unit` (auth), `test_mailbox` (mailbox and
  isolation), `test_card` (discovery), `test_config` (config), `test_executor_unit`
  (executor/resolver), `test_app` (fail-closed startup, lifecycle), `test_server` (startup),
  `test_security` (body-size limit, token redaction), `test_client` (client + CLI driven
  against the real app), `test_sessions` (per-session identities, mandatory session).

## Running the service

The service is **self-contained**: token auth is in-process, so no reverse proxy or
Kubernetes is required.

- Docker: `docker build -t a2a-hub . && docker run -p 8000:8000 -e A2A_HUB_TOKENS="tok:agent" a2a-hub`.
- HTTPS directly: set `A2A_HUB_TLS_CERTFILE`/`A2A_HUB_TLS_KEYFILE` (built-in uvicorn TLS), or
  front it with any TLS-terminating proxy.
- Persistence: mount a volume for the SQLite file (`A2A_HUB_DB_PATH`), or point
  `A2A_HUB_DB_URL` at PostgreSQL.

### CI (GitHub Actions)

- `.github/workflows/ci.yml`: job `test` (gate, `uv run pytest`, coverage ≥ 90 %) and job
  `publish` (only push to `main`) that builds+pushes the image to GHCR
  (`ghcr.io/tlmak0/a2a-hub`, `:latest` + immutable `:sha-<commit>`).
- This repo does **not** deploy. Kubernetes manifests and the deploy pipeline live in a
  **separate private infra repo** that consumes this GHCR image. The `publish` job optionally
  notifies that repo (`repository_dispatch`) when `MYINFRA_DISPATCH_TOKEN` is set.

## The hub is shared infrastructure: don't break the contract silently

It has live clients (other agents). Treat the wire contract — required headers,
`recipient` format, identities, method names — as public API:

- **Land changes through a PR with green CI**, never a direct push to `main`.
- **Announce breaking changes before deploying them**, and prefer a backwards
  compatible transition (accept both shapes for a while) over a hard cut-over.
- **A test must encode the real use case, not just the code path.** The session change
  shipped at 100% coverage and still broke store-and-forward, because no test covered
  "A writes to B without knowing B's session; B connects later and receives it".
  That test now exists (`test_sessions.py`); coverage is a floor, not the goal.
- Changing storage semantics can strand existing rows. Check what is already in the
  task store before changing how owners are resolved.

## Conventions

- Commits with a scope prefix (`feat(server):`, `fix(auth):`, `docs:`), in English.
- Project knowledge lives in these docs (`README.md`, `AGENTS.md`, `CLAUDE.md`), not in any
  separate memory store.
