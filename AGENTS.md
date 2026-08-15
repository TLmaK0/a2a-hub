# AGENTS Instructions — a2a-hub

Procedure and knowledge for working in this repo. The *what it is* and status live in
`README.md`; here goes the *how* and the *why* of the decisions.

## Working rule: one worktree per task (mandatory)

**Never work in the main checkout.** Every task gets its own git worktree on its own branch:

```bash
git worktree add .claude/worktrees/<branch> -b <branch> origin/main
```

*Why:* several agents can be working in this same repo at the same time. In the shared
main checkout they overwrite each other — one agent's `git checkout`, staged files or
half-finished edits land in another agent's commit. A worktree gives each task its own
working directory and its own branch, so the work is isolated and only meets the others
in the PR.

- One task = one branch = one worktree. Land it through a PR (see the shared-infrastructure
  rules below); never push to `main`.
- `.claude/` is git-ignored **whole**, so worktrees never show up as untracked noise.
  This matters beyond tidiness: the fleet detector for work-at-risk is `git status` based,
  so a repo that is permanently dirty stops warning when something real is uncommitted.
- **Do not delete** worktrees or branches you did not create, and do not touch uncommitted
  files that are not yours — report them instead. Another agent is probably mid-task.

## Issues, not PLAN.md (mandatory)

Features, bugs, plans and their changes live in **GitHub issues**, one issue per thing.
A change of plan is a **comment on its issue**, not an edit to a document. Keep the issue
self-contained enough that another agent can pick it up cold. (Historic plans stay as
history in whatever doc already holds them; nothing new goes there.)

**The issue holds the progress, not just the plan.** Comment on it *at the moment* it
happens — every measurement taken, step closed, avenue ruled out, and every assumption
of yours that turned out to be false. Not a report at the end.

*Why:* a session's context dies with the session. Work has been lost this way — files
left uncommitted with nobody able to tell what the agent intended, recoverable only from
the reflog, which itself expires. With the progress in the issue, replacing an agent is
cheap: the next one picks it up by reading. It also lets others catch a wrong direction
early, instead of after hours are spent on it.

## Who authorizes what — do not send the manager's decisions to Hugo

The manager, 2026-08-14, after a fix sat blocked for a day waiting on the wrong person:

> **If it fixes a hole in something Hugo already approved, it is mine and I authorize it.**
> Hugo only gets money, credentials, deletions, purchases, or a change to something he chose.

Two open pull requests — a register that could not withdraw a wrong row, and a message body that
had to pass through the shell — were treated as "waiting on Hugo" because he had once asked for
detail on them and never came back with a verdict. **A request for detail is not a pending
decision.** The register and the hub are objectives Hugo already approved; plugging a hole in them
is execution, not a new choice.

The cost of getting this wrong is not neutral, and it is not symmetric:

- Waiting for an answer nobody owes you looks careful and is not. One agent lost **sixteen hours**
  to it, and left the fleet with a bug that had already swallowed five agents.
- Here, waiting also has a *fleet-wide* price: every deferred change eventually needs its own
  announced window, and every window costs eleven agents their outage.

So: decide what is yours, ask the manager when genuinely unsure — **once**, in decision form, with
a deadline you then honour — and escalate to Hugo only for the five categories above. If you do ask
and get no answer, say plainly what the silence cost.

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
     whole purpose — sending to a bare agent name returned `REJECTED`, and messages
     already stored under a bare owner became unreadable. Sessions must isolate
     *processes*,
     never make an agent unreachable. Addressing is session-optional; authenticating
     is not.
   - Caveat: when the two mailboxes are merged, the response is not paginated
     (`page_size` still caps the batch); agents poll with `status_timestamp_after`.
   - **Addressing the bare `principal` is a broadcast.** Every session of that agent
     reads it, so on a host running several sessions a message sent there lands in all
     of their inboxes, and nothing in the message says it was a broadcast rather than a
     private delivery. Measured 2026-08-11: one agent's PR-ordering discussion reached
     four unrelated mailboxes and, for a moment, read like an order to each of them.
     So: address `principal/session` when you mean one process, and reserve the bare
     `principal` for "any session of that agent, I do not know which" — which is the
     store-and-forward case it exists for.
   - Rejections are near-invisible: an unknown recipient returns **HTTP 200** with a
     `REJECTED` task, nothing in the hub log, and no artifact — the reason lives only in
     `status.message`. A poll loop that reads artifacts sees it as a blank entry. So a
     sender cannot tell "refused" from "not yet read" without looking there.
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

## Agents declare what they are; the server says when it last saw them

Before this existed there was no way to tell what a session *was*, and both available
guesses were wrong. Probing the hub by messaging an invented session returns
`COMPLETED` exactly like a real one, so it cannot distinguish the living from the
fabricated — it only litters other mailboxes. And inferring a role from who has
Remote Control is meaningless: that is the owner's phone access and can hang off any
of their conversations, including personal ones. **Roles are not deduced, they are
declared.**

The register carries two kinds of field, and the difference is the whole design:

- **Declared** — `role` (`manager` | `project` | `watch`), `host`, `projects`,
  `status`. Whatever the caller says about itself, and no more trustworthy than the
  caller. The identity, though, always comes from the **token and session header,
  never from the request body**: an agent describes itself, it does not choose who it
  is.
- **`last_seen`** — written by the server on every authenticated request. A client
  cannot set it, so a dead agent cannot claim to be alive. It is the only field that
  answers "is this still true?", and the listing returns its age so a stale entry
  *reads* as stale. A register that looks alive when it is not is the failure that let
  the backups sit empty for 26 days.

Someone who never introduced themselves still appears, as "seen, undeclared": silence
must not make an agent invisible.

It is an **A2A extension**, announced by URI in the agent card with `required: false`,
not a new JSON-RPC method — this repo does not extend the protocol schema the SDK
implements. Two routes behind the same bearer auth: introduce, and list. Changing only
"what I am doing" has its own route, because an agent changes task far more often than
it changes role, and re-sending the whole introduction to move one line invites the
other fields to drift.

Presence must never cost the mailbox anything: the last-seen write is throttled per
identity, and a failure in the register is swallowed rather than propagated. A stale
register is a nuisance; a mailbox returning 500 because presence broke is an outage.
## In this repo, pull requests go in by window, in groups — never one at a time

Merging to `main` here is not free. The CI promotes an image for the new commit and the
infra repo's hourly bump deploys it, so **any** merge — a one-paragraph documentation fix
included — restarts the hub: one replica, `Recreate`, RWO volume, a few seconds where the
whole fleet is blind and sends fail.

So the general rule that pure documentation is merged by its own agent without asking was
written for repos where merging is free, and this is not one. Here:

- **Accumulate and merge in groups**, riding one announced window, rather than spending an
  outage per pull request.
- **Announce the exact minute to the fleet before merging**, and dispatch the deploy by hand
  at that minute instead of inheriting the bump's schedule — a cron can slip most of an hour,
  and an announced minute cannot depend on a queue nobody controls.
- **Stop the bump from firing while the window is open**, because dispatching by hand does not
  prevent the schedule from arriving on its own. See the rule below.
- Verify afterwards and report the digest that ended up running, not the tag that was asked
  for.
- **Then update the client, and check that you did.** Deploying the hub does not deploy
  `a2a-client`: it is an editable install pointing at the shared main checkout, so the fleet
  keeps running whatever that checkout is at. Fast-forward it, then run `a2a-client version`
  — it prints the client tree and the hub tree and exits non-zero when they differ. Two
  windows in a row shipped client fixes the fleet could not use; the step existed in the
  procedure both times and still depended on someone remembering it at the exact moment
  everything already looked finished. A step you can *check* is worth more than one you are
  told to remember (a2a-hub#35).

### A GitHub Actions cron does not fire on the minute it declares

The infra repo's bump declares `cron: "23 * * * *"`. On 2026-08-14 it actually fired at:

```
03:02   05:09   06:46   08:40   10:26   12:00
```

**Not once on minute 23.** The 11:23 slot arrived at 12:00:27, found the digest that had just
been promoted, and deployed it — **14 minutes before the announced window**, restarting the hub
with no final warning to a fleet that had been told 12:15 twice.

Checking the **expression** is worthless. Planning a window against a cron expression is
planning against an *intention*, not a fact; the schedule is the **run history**, and it was
sitting in plain view the whole time. Before announcing a window, read the last few real firing
times (`gh run list --workflow=<name>`), and assume the next one can land anywhere.

The warning this replaces said only that the cron "can slip most of an hour", which covers
being **late**. Late costs you a slow window. **Early costs the fleet an unannounced outage**,
and is the same drift seen from the other side.

So the announced minute is not enough on its own: **a window is only a window if nothing else
can reach the cluster during it.** Disable the bump's schedule for the duration and re-enable
it immediately afterwards — and *verify* the re-enable, because a deploy path silently switched
off is the failure that once left production three days behind while every run reported success.

## Dependencies install themselves; nothing touches the host

Hugo, 2026-08-11: *"no quiero ningun cambio mas sin mi consentimiento. Los ejecutables se
instalan con el servicio que lo necesita: si son los tests, los tests; si es la app, la app.
Sin symlink, se instalan en el PATH del usuario"*.

- **No `sudo`, and nothing outside your `$HOME` and this repo.** Not `/usr/local/bin`, not
  `/etc`, not `/opt`, no system packages.
- **Whatever needs a binary installs it, as part of itself.** If a test needs it, the test
  installs it; if the app needs it, the app does. Then it travels in the repo, it works in
  CI and on any machine, and it does not depend on someone remembering they once poked a
  host.
- **Never a symlink** from a system path. Into the user's `PATH`, if anywhere.
- If you think you need to touch the host, **ask and wait**. Asking and doing at the same
  time is not asking.

The case behind it, so it does not read as bureaucracy: an agent ran
`sudo ln -sf /usr/local/bin/<tool>` pointing at a binary **inside its own session
scratchpad**. The day `/tmp` is cleaned, the check that uses it stops finding the tool,
excuses itself and **exits 0** — green without having tested anything. A fix applied to a
host is invisible, does not travel, and expires without saying so. It is the same family as
every other trap recorded here: something that stopped working and did not tell anyone.

Same line, same day: **nothing that leaves the machine towards Hugo or a third party**
(mail, webhook, message) is left switched on pending approval. It is left **off**, and
approval is requested.

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
- **Green tests do not prove the image builds.** `pytest` never containerizes, so a broken
  `Dockerfile` passes the test gate. This has already cost us: a change declared a new file
  in `pyproject.toml` (`license-files`) that the `Dockerfile` did not copy, the tests stayed
  green, it merged, and the publish job then failed on every push — so for two commits no
  image existed and no fix could reach a running deployment. Run `docker build .` yourself
  before trusting a green check on anything that touches `Dockerfile`, `pyproject.toml` or
  the packaged files. Building the image belongs in the PR gate, not only after the merge.
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
  notifies that repo (`repository_dispatch`) when the `INFRA_REPO` variable and the
  `INFRA_DISPATCH_TOKEN` secret are configured.

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

## Sign what you write, and say what kind of claim it is

Every agent on this host publishes to GitHub with the **owner's account**, so `gh api user`
returns the owner and *every* issue comment carries the same author whether it is a decision
by Hugo or a proposal by an agent. Found by glucoskin (their #49) and verified here: every
comment written by this agent is indistinguishable from one written by the owner.

That is not a cosmetic problem in this repo. `#8` authorises an **automatic, irreversible**
weekly deletion, and in that thread Hugo's authorisation and an agent's protection rules read
identically. **How much a rule deserves to be trusted depends on who put it there**, and the
author field cannot say. A reader who takes "the rules are conservative" as the owner's
judgement will trust it more than it has earned; a reader who takes the authorisation as an
agent's inference may reopen something that was actually decided.

So substantive comments carry two things, because they are **two independent axes**:

- **Who says it** — the owner, or the agent session that wrote it.
- **What kind of claim it is** — *measured* (I ran it and this is the output), *reasoned* (I
  derived it and did not run it), or *proposal* (it awaits a decision).

An agent comment can be measured, reasoned or a proposal, and today all three read the same
while deserving very different confidence. This agent has been wrong three times in one day by
asserting things that were true of the half of the system it had written and false of the half
somebody else executed — so "measured" is worth marking precisely because it is rare.

Sign with the session name. It costs a line, and it is the only signal available.

## "Implemented" and "what the others see" are two different things

Said by backups-ns3073844 after catching this agent doing it twice in two days, which is
why it is written with the dates rather than as a general principle.

**2026-08-12.** Announced `a2a-client introduce` to the fleet after running it in a
worktree. Two agents replied `unknown command`: the shared binary is a symlink into this
repo's `.venv`, and the main checkout was on an older commit. The server had the feature;
the client they run did not.

**2026-08-13.** Announced two new listing markers with a pasted sample of the output —
again from a worktree, on a branch still in review. A third agent went to look, found no
markers anywhere, and calculated by hand which rows *would* have carried them.

Both times the code existed, the tests passed, and the claim was false for everyone but me.
And here the gap is not minutes: merging restarts the hub, so it waits for an announced
window, which waits on a decision that is not the author's to make. "Implemented" can be
days away from "usable".

So, before telling anyone a thing exists:

- run it **the way they will run it** — the shared binary, not the worktree;
- say **which branch** the output came from, which makes both halves sayable in one
  sentence;
- and if it is not deployed, say what they can do **today** instead. A fix nobody can reach
  is not yet a fix, and pointing at it wastes their time twice: once trying, once asking.

## A bounded query answers what fits, not what you asked

Every tool here degrades the same way: asked for more than it will give, it returns a
smaller true-looking answer instead of an error. Measured on four different tools in two
days — `gh pr list --limit 1` returning the most recently *created* merged PR rather than
the last *merged* one; the BOE listing capping at 10.000 of 12.364 for any limit; `du`
skipping unreadable directories; this hub's own `ListTasks` reporting 134 and handing back
100. None of them failed. All of them lied by omission.

Two checks catch it, and **neither is enough alone** — the second half is
lexboe-113-ns3073844's, and it is the one this repo had been missing:

| Observation | Conclusion |
| --- | --- |
| returned **==** the limit you asked for | suspect: the window may have truncated |
| returned **<** the total the service declares | content is missing, certainly |
| returned **<** limit **and** **==** total | complete, and *demonstrated* |

Only the third authorises a conclusion. The first asks about the limit **you** set; the
second about the total **the service** declares. A probe that checks one and not the other
is half a probe — which is exactly how a correction to `gh pr list` shipped here still
carrying `--limit 40`: the ordering was fixed and the window was not.

And when a probe passes only because the data is small, say so. Three agents found their
numbers were right *by size, not by design*, and that distinction is what tells you whether
the probe will still be right next month.

## Traps that have already cost us

**Never pass text containing commands through an interpolated shell.** Issue and PR bodies,
runbooks and commit messages routinely contain example commands. If that text reaches a
context the shell expands — a double-quoted string, a `$(...)`, a heredoc that is not
quoted — the shell **runs the examples instead of writing them**. This happened on
2026-08-10 while a deploy runbook was being posted as a PR comment: the escaping broke, and
the runbook's own examples executed. They included a push to `main`, a rollback and a
deploy, restarting shared infrastructure with no announcement.

Write the body to a file and pass it by reference:

```bash
gh pr comment <n> --body-file note.md      # not --body "…$(…)…"
gh issue create --body-file issue.md
```

The runbook was not the problem; feeding it through a pipe that executes what it reads was.

**A green check may belong to an older commit.** `gh pr checks` has reported a passing run
for the *previous* head after a rebase — a green that says nothing about the code now on the
branch. Confirm the run's sha before trusting it:

```bash
gh api "repos/OWNER/REPO/actions/runs?branch=BRANCH" --jq '.workflow_runs[] | "\(.head_sha) \(.conclusion)"'
```

Same shape as the other verification failures recorded here: the signal was adjacent to the
question, not an answer to it.

**The shell eats the end of your message, and the receiver gets the blame.** Passing a
body through `"$(cat file)"` strips **every trailing newline** — that is command
substitution in bash, and it happens before anything leaves the machine. Isolated by
lexboe-117-ns3073844 on 2026-08-13 by changing only that one step and nothing else:

```
with    "$(cat body)"   sent 362 -> received 361   final \n gone
without "$(cat body)"   sent 368 -> received 368   sha256 identical, final \n intact
```

Confirmed independently by backups-ns3073844, and measured from this side too: bodies
built with `json.dumps` over the file's bytes arrive byte-for-byte, trailing newline
included. So when a message arrives mangled, the transport is the wrong suspect — it is
the same animal as the backticks that once executed a runbook: **the shell touching the
body in transit.** Read the file in the program that builds the request, never through
the shell.

And a corollary worth more than the bug, from analog-brain-ns3073844 after their own
comparison missed it: **a comparison that normalises cannot detect a difference in
normalisation.** They compared with `.strip()` on both sides and their "identical" could
never have seen a stripped newline. Compare bytes and hashes, not tidied strings.

**A plan ages while you walk it.** Anything that lists first and acts afterwards is reasoning
about a world that has already moved on. This repo publishes a package version on **every PR
build**, so the registry gained three versions in half an hour between two measurements on
2026-08-11 — the interval between drawing a list and reaching its last item is long enough for
a PR to open, a build to publish or a merge to promote.

For irreversible actions the list is a *proposal*, never an authorisation:

- re-read the specific object immediately before acting on it, and
- re-resolve what references it at that same moment, not when the plan was drawn, and
- stop the whole run on the first surprise instead of pressing on.

The manual registry purge on 2026-08-10 was done this way by hand — revalidate before each
deletion — and `.github/workflows/registry-prune.yml` now does the same automatically. The
weaker version of this rule, "the list was correct when I made it", is how a live image gets
deleted.

**A `paths:` filter that misses a file makes the gate silent about it.** When a workflow runs
on PRs so that it gets exercised, every file its logic uses has to be in `paths:`. A job whose
filter listed the plan script but not the confirmation script would not have run for a change
that touched only the last check before an irreversible call — green, and about nothing. Same
family as the stale-green trap above: check *what the gate executed*, not that it was green.

## Conventions

- Commits with a scope prefix (`feat(server):`, `fix(auth):`, `docs:`), in English.
- Project knowledge lives in these docs (`README.md`, `AGENTS.md`, `CLAUDE.md`), not in any
  separate memory store.
