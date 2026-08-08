# a2a-hub

**A2A (Agent2Agent)** server for direct communication between agents.

Agents (e.g. Claude Code sessions on different machines) need to talk to each other without
relying on a shared git repo as a mailbox. This project runs a server that speaks the
**open A2A protocol** (Linux Foundation) so any compatible agent can send and receive
messages over HTTPS with authentication.

## Why it exists

- There is no *turnkey*, maintained, self-hosted A2A server to act as a meeting point. The
  `Agent-2-Agent/a2a-server` repo is dead (1 commit, no real protocol) — discarded.
- The living, serious piece is the **official [`a2a-sdk`](https://github.com/a2aproject/a2a-python)**
  (FastAPI/Starlette, SQL persistence, A2A operations). This repo is the minimal layer
  (`AgentExecutor` + service) on top of that SDK. We do not reimplement the protocol.

**Self-contained:** authentication is enforced in-process (bearer token), so the service
needs no reverse proxy or Kubernetes to be secure. Run it with Docker and a set of tokens;
optionally point it at a TLS cert to serve HTTPS directly.

## Architecture

- **Base:** official `a2a-sdk`. HTTP server (Starlette/FastAPI).
- **Hub / store-and-forward topology.** The agents are loops that *poll*, not always-listening
  servers. The server **persists tasks** (TaskStore) and each agent runs `ListTasks`/`GetTask`
  on its tick to pick up what was left for it. This uses the standard A2A Task model — not a
  home-grown protocol.
- **Persistence:** SQLite to start (a single file); PostgreSQL if it grows.
- **Auth:** bearer token per agent (one token = one agent), declared in the Agent Card.
- **Discovery:** Agent Card at `/.well-known/agent-card.json`.

## Run with Docker

The service is self-contained — Docker and a set of tokens are all you need:

```bash
docker build -t a2a-hub .
docker run -p 8000:8000 -e A2A_HUB_TOKENS="tok-a:agent-a,tok-b:agent-b" a2a-hub
# HTTPS directly (self-contained, no proxy):
docker run -p 8443:8443 \
  -e A2A_HUB_TOKENS="tok-a:agent-a" \
  -e A2A_HUB_PORT=8443 \
  -e A2A_HUB_TLS_CERTFILE=/certs/tls.crt -e A2A_HUB_TLS_KEYFILE=/certs/tls.key \
  -v /path/to/certs:/certs:ro a2a-hub
```

CI publishes the image to GHCR (`ghcr.io/tlmak0/a2a-hub`) on every green merge to `main`.

Kubernetes manifests and the deploy pipeline live in a **separate private infra repo**, not
here — this repo is just the service.

## Usage

```bash
uv sync                                              # environment + dependencies
A2A_HUB_TOKENS="tok-a:agent-a,tok-b:agent-b" \
  uv run a2a-hub                                     # starts on :8000
uv run pytest                                        # tests (coverage ≥ 90%)
```

Configured via the environment (see `.env.example`). Mailbox flow over JSON-RPC (the
`A2A-Version: 1.0` header is required):

```bash
# agent-a leaves a message for agent-b
curl -s localhost:8000/ -H "Authorization: Bearer tok-a" -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":
       {"message":{"messageId":"m1","role":"ROLE_USER",
        "parts":[{"text":"hello B"}],"metadata":{"recipient":"agent-b"}}}}'

# agent-b picks up its mailbox (agent-a sees nothing in its own)
curl -s localhost:8000/ -H "Authorization: Bearer tok-b" -H "A2A-Version: 1.0" \
  -d '{"jsonrpc":"2.0","id":2,"method":"ListTasks","params":{"includeArtifacts":true}}'
```

## Status

✅ Working self-contained service: mailbox executor, persistent TaskStore, bearer auth, Agent
Card, Dockerfile, optional built-in TLS. Functional tests at **100 % coverage** (90 % enforced
minimum). Architecture and testing details in `AGENTS.md`.

## Roadmap

- [x] Minimal executor on `a2a-sdk` (mailbox: `SendMessage` + `ListTasks` + `GetTask`).
- [x] Persistent SQLite TaskStore.
- [x] Bearer token auth middleware (401 if missing/invalid token).
- [x] Agent Card at `/.well-known/agent-card.json`.
- [x] Per-feature functional tests with coverage ≥ 90 %.
- [x] Self-contained Docker image with optional built-in TLS.
- [x] CI: test gate + build/push image to GHCR.
- [ ] Example client for an agent's loop (curl and/or MCP).
