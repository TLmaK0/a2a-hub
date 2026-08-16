"""Minimal A2A client for an agent's poll loop against the hub.

Agents are loops that *poll*: they leave messages in someone's mailbox with
``SendMessage`` and pick up their own with ``ListTasks``/``GetTask``. This module is
the reference client for that loop — no extra dependencies, stdlib only.

Configuration comes from the environment, or from an env-style file (default
``~/.config/a2a-hub/agent.env``) so a token never has to live in the repo::

    A2A_HUB_URL=https://a2a.example.com/
    A2A_HUB_AGENT=my-agent
    A2A_HUB_TOKEN=<my bearer token>
    A2A_HUB_SESSION=<optional session name>

Sessions: the token identifies the machine (principal). Setting a session gives this
process its own mailbox, ``principal/session``, so several sessions on the same
machine can message each other. Address them as ``machine/session``.

CLI::

    a2a-client whoami
    a2a-client inbox [--json]
    a2a-client read <task-id>
    a2a-client send <recipient> <text...>
    a2a-client send <recipient> --body-file <path>   # bodies with commands in them
    a2a-client introduce <role> <project[,project...]> <what you are doing...>
    a2a-client status <what you are doing...>
    a2a-client retire
    a2a-client agents [--json] [--quiet-for SECONDS] [--retired]
    a2a-client [--session NAME] ...      # overrides A2A_HUB_SESSION
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import sys
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


#: Default location of the agent credentials file.
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "a2a-hub" / "agent.env"

#: Protocol version required by the hub on every JSON-RPC request.
A2A_VERSION = "1.0"

#: Callable that performs the HTTP POST and returns the decoded JSON-RPC response.
Transport = Callable[[str, bytes, dict[str, str]], dict[str, Any]]


class ClientError(RuntimeError):
    """Configuration or hub-side error surfaced to the caller."""


def _parse_env_file(path: Path) -> dict[str, str]:
    """Read a ``KEY=value`` file, ignoring blanks and ``#`` comments."""
    values: dict[str, str] = {}
    try:
        content = path.read_text()
    except OSError:
        return values
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class ClientConfig:
    """Where the hub is, who I am, and the token that proves it.

    Attributes:
        url: base URL of the hub's JSON-RPC endpoint.
        agent: this agent's (principal) name; identity comes from the token.
        token: bearer token for this agent.
        session: per-session name; required, gives this process its own mailbox.
    """

    url: str
    agent: str
    token: str
    session: str
    #: True when the session was read from the shared config file rather than set
    #: for this process. Registering under a shared session is refused.
    session_from_shared_file: bool = False

    @property
    def identity(self) -> str:
        """Full mailbox identity: ``agent/session``."""
        return f"{self.agent}/{self.session}"

    @classmethod
    def load(
        cls,
        environ: Mapping[str, str] | None = None,
        config_path: Path | None = None,
    ) -> ClientConfig:
        """Build the config from the environment, falling back to the config file.

        Environment variables win, so a container can override the file.
        """
        env = dict(os.environ if environ is None else environ)
        path = DEFAULT_CONFIG_PATH if config_path is None else config_path
        merged = {**_parse_env_file(path), **{k: v for k, v in env.items() if v}}

        missing = [
            key
            for key in (
                "A2A_HUB_URL",
                "A2A_HUB_AGENT",
                "A2A_HUB_TOKEN",
                "A2A_HUB_SESSION",
            )
            if not merged.get(key)
        ]
        if missing:
            raise ClientError(
                f"missing {', '.join(missing)} (set them in the environment or {path})"
            )
        # Where the session came from decides whether registering under it is safe:
        # the config file is shared by every process on the host, so a session read
        # from it is not this agent's identity, it is the host's default. Registering
        # under it silently overwrites whoever registered last — which is exactly how
        # a manager's entry was replaced by another agent on 2026-08-11.
        return cls(
            url=merged["A2A_HUB_URL"],
            agent=merged["A2A_HUB_AGENT"],
            token=merged["A2A_HUB_TOKEN"],
            session=merged["A2A_HUB_SESSION"],
            session_from_shared_file=not (env.get("A2A_HUB_SESSION") or "").strip(),
        )


def _urllib_transport(url: str, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """Default transport: stdlib POST returning the decoded JSON body."""
    request = urllib.request.Request(url, data=body, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.load(response)


class HubClient:
    """Thin JSON-RPC client for the hub's mailbox operations."""

    def __init__(
        self, config: ClientConfig, transport: Transport | None = None
    ) -> None:
        """Args:
        config: hub URL, agent name and bearer token.
        transport: override the HTTP layer (used by tests).
        """
        self.config = config
        self._transport = transport or _urllib_transport

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one JSON-RPC call and return its ``result``."""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "A2A-Version": A2A_VERSION,
            "Authorization": f"Bearer {self.config.token}",
            # Claims this session's own mailbox under our token's principal.
            "A2A-Session": self.config.session,
        }
        payload = self._transport(self.config.url, body, headers)
        if "error" in payload:
            error = payload["error"]
            message = (
                error.get("message", error) if isinstance(error, dict) else error
            )
            raise ClientError(f"hub error: {message}")
        return payload["result"]

    def _http(self, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """Call one of the register routes.

        These are not JSON-RPC: the register is an announced A2A *extension*, so it
        lives on its own paths behind the same bearer auth.
        """
        url = self.config.url.rstrip("/") + path
        headers = {
            "Content-Type": "application/json",
            "A2A-Version": A2A_VERSION,
            "Authorization": f"Bearer {self.config.token}",
            "A2A-Session": self.config.session,
        }
        payload = self._transport(
            url, json.dumps(body).encode() if body is not None else None, headers
        )
        if "error" in payload:
            raise ClientError(f"hub error: {payload.get('detail', payload['error'])}")
        return payload

    def introduce(
        self, role: str, host: str, projects: list[str], status: str
    ) -> dict[str, Any]:
        """Say who I am and what I am doing. Identity comes from the token."""
        return self._http(
            "/agents/register",
            {"role": role, "host": host, "projects": projects, "status": status},
        )

    def set_status(self, status: str) -> dict[str, Any]:
        """Move only the "what I am doing" line of an existing introduction."""
        return self._http("/agents/status", {"status": status})

    def retire(self) -> dict[str, Any]:
        """Withdraw my registration. Retired, not deleted."""
        return self._http("/agents/retire", {})

    def agents(
        self, quiet_for: int | None = None, *, retired: bool = False
    ) -> dict[str, Any]:
        """Who else is connected, what they are, and when they were last seen.

        With ``quiet_for``, only those the hub has not heard from for that many
        seconds — the question "who has gone quiet?", asked rather than eyeballed.

        With ``retired``, withdrawn rows come too. The route has always accepted it
        and this client never asked, which made ``retired_at`` a field that is
        rendered and can never be anything but null — information-shaped, and
        incapable of carrying information. Reported by glucoskin-ns3073844 after the
        only question that needed it ("has anyone introduced themselves under this
        retired name?") could be answered solely by hand-writing the HTTP call.
        """
        params = []
        if quiet_for is not None:
            params.append(f"quiet_for={quiet_for}")
        if retired:
            params.append("retired=true")
        path = "/agents" + ("?" + "&".join(params) if params else "")
        return self._http(path, None)

    def list_tasks(
        self,
        include_artifacts: bool = True,
        *,
        page_size: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """List **one page** of the tasks waiting in *my* mailbox."""
        params: dict[str, Any] = {"includeArtifacts": include_artifacts}
        if page_size is not None:
            params["pageSize"] = page_size
        if page_token:
            params["pageToken"] = page_token
        return self._rpc("ListTasks", params)

    def list_all_tasks(
        self,
        include_artifacts: bool = True,
        *,
        page_size: int = 100,
        max_pages: int = 100,
    ) -> dict[str, Any]:
        """Follow the page tokens to the end, which is the only honest way to read.

        A single ``ListTasks`` answers what fits, not what you asked for. Reading one
        page and stopping is how a mailbox of 258 looked like a mailbox of 50 — with
        no error to notice, because the server used to hand back an empty token while
        there was more.

        Returns a page-shaped dict carrying every task, plus ``pagesRead`` and, if the
        walk was cut short, ``incomplete``. It stops **without raising**: half a
        mailbox plus a warning beats an exception and no mailbox at all.
        """
        tasks: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        token, pages, total, incomplete = "", 0, 0, False
        while True:
            page = self.list_tasks(
                include_artifacts, page_size=page_size, page_token=token or None
            )
            pages += 1
            tasks.extend(page.get("tasks", []))
            total = int(page.get("totalSize", 0))
            token = page.get("nextPageToken") or ""
            if not token:
                break
            # A repeated token means the hub is walking in a circle; a page cap means
            # it is handing out more pages than any real mailbox needs. Either way,
            # stop — an agent loop must not spin on someone else's bug.
            if token in seen_tokens or pages >= max_pages:
                incomplete = True
                break
            seen_tokens.add(token)
        result: dict[str, Any] = {
            "tasks": tasks,
            "totalSize": total,
            "nextPageToken": "",
            "pagesRead": pages,
        }
        if incomplete:
            result["incomplete"] = True
        return result

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Fetch a single task from my mailbox by id."""
        return self._rpc("GetTask", {"id": task_id})

    def send_message(self, recipient: str, text: str) -> dict[str, Any]:
        """Leave a text message in ``recipient``'s mailbox."""
        return self._rpc(
            "SendMessage",
            {
                "message": {
                    "messageId": os.urandom(8).hex(),
                    "role": "ROLE_USER",
                    "parts": [{"text": text}],
                    "metadata": {"recipient": recipient},
                }
            },
        )


def _appears_in_register(hub: HubClient, identity: str) -> bool:
    """Read back what we just wrote, so a silent no-op cannot pass as success.

    Deliberately tolerant of its own failure: if the listing itself cannot be read we
    return ``True`` rather than accusing a healthy introduction of having vanished. A
    check that produces false alarms gets ignored, and then it protects nothing.
    """
    try:
        listed = hub.agents().get("agents", [])
    except ClientError:
        return True
    return any(entry.get("identity") == identity for entry in listed)


def format_task(task: dict[str, Any]) -> str:
    """One-line summary of a task plus the messages it carries."""
    status = task.get("status", {})
    state = status.get("state", "?").replace("TASK_STATE_", "")
    when = status.get("timestamp", "")[:19].replace("T", " ")
    lines = [f"[{task.get('id', '?')[:8]}] {state:<9} {when}".rstrip()]
    for artifact in task.get("artifacts", []):
        sender = artifact.get("metadata", {}).get("sender", "?")
        text = " ".join(
            part.get("text", "") for part in artifact.get("parts", [])
        ).strip()
        lines.append(f"    from {sender}: {text}")
    return "\n".join(lines)


def format_agent(agent: dict[str, Any]) -> str:
    """One line per agent, with the age up front.

    The age leads because it is the only field the server vouches for: everything
    else is what that agent said about itself, and a register that cannot go stale
    has not been built yet.
    """
    age = agent.get("last_seen_seconds")
    if age is None:
        seen = "never seen"
    elif age < 120:
        seen = f"{age}s ago"
    elif age < 7200:
        seen = f"{age // 60}m ago"
    else:
        seen = f"{age // 3600}h ago"

    if not agent.get("declared"):
        return f"[{seen:>9}] {agent['identity']} (undeclared)"

    # Two different freshnesses, and conflating them misled three agents at once on
    # 2026-08-13: a manager seen 3 h ago still carried yesterday's status text, and the
    # fleet read "tick 12-08" as evidence it had stopped. `seen` is stamped by the
    # server and answers "is it alive"; the status is the agent's own words and answers
    # "what was it doing WHEN IT SAID SO". Showing only one makes the other look current.
    said = _age(_status_age_seconds(agent))
    stale = f" (said {said})" if _status_is_older(agent) else ""
    seconds = agent.get("last_seen_seconds")
    silent = " [SILENT]" if seconds is not None and seconds > SILENT_AFTER else ""

    projects = ",".join(agent.get("projects") or []) or "-"
    # A withdrawn row looks exactly like a live one otherwise, and it is the row that
    # matters most when you go looking: an identity that was retired cannot be
    # re-declared on an older hub, so introducing yourself under it leaves you
    # invisible while the call returns 200.
    withdrawn = " [RETIRED]" if agent.get("retired_at") else ""
    return (
        f"[{seen:>9}]{silent}{withdrawn} {agent['identity']} {agent.get('role')} "
        f"{projects} ::{stale} {agent.get('status') or '-'}"
    )


def _age(seconds: int | None) -> str:
    """Human-readable age, or "never" when there is nothing to age."""
    if seconds is None:
        return "never"
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _status_age_seconds(agent: dict[str, Any]) -> int | None:
    """How old the agent's own words are, derived from declared_at."""
    declared, last_seen = agent.get("declared_at"), agent.get("last_seen")
    if not declared or not last_seen or agent.get("last_seen_seconds") is None:
        return None
    from datetime import datetime

    fmt = lambda v: datetime.fromisoformat(v.replace("Z", "+00:00"))  # noqa: E731
    return int(
        (fmt(last_seen) - fmt(declared)).total_seconds()
    ) + int(agent["last_seen_seconds"])


#: How much older the words may be than the sighting before it is worth saying.
#: Calibrated against the live register rather than guessed: lexboe-117-ns3073844
#: measured that an hour would flag 7 of 10 rows — "sale siempre", which is the
#: failure this margin exists to avoid. The real signal sat in the top three (41 h,
#: 18 h, 16 h) while 1.5-4 h was ordinary drift from agents that are working and have
#: simply not restated themselves. Six hours keeps the three and drops the four.
STATUS_STALE_AFTER = 6 * 3600

#: How long a silence has to be before the listing says so. Different question,
#: different clock, different source — the mistake this file already made once.
#:
#: "Are its words current?" compares declared_at with last_seen, both about what the
#: agent chose to say. "Is anything running?" is only about last_seen, which the
#: server stamps on every authenticated call, and its natural norm is this host's
#: poll interval: measured across ten live rows, seven were under 15 minutes and the
#: only outlier was at 3.6 h. One hour is four times the norm — late enough that a
#: working agent never trips it, early enough to be the first to notice.
#:
#: What it detects, precisely, is a STOPPED LOOP and nothing else. backups-ns3073844
#: put it exactly: an agent does not call because it is alive, it calls because a tick
#: woke it. If the loop dies the field freezes exactly as if the agent had — and the
#: agent cannot report that, because it is not running to report anything.
SILENT_AFTER = 3600


def _status_is_older(agent: dict[str, Any], margin: int = STATUS_STALE_AFTER) -> bool:
    """Whether the words are meaningfully older than the last sighting.

    Only flagged past a margin: an agent that updates its status on every tick would
    otherwise carry a permanent marker, and a marker everyone sees is a marker nobody
    reads.
    """
    said = _status_age_seconds(agent)
    seen = agent.get("last_seen_seconds")
    return said is not None and seen is not None and said - seen > margin


def _quiet_for_arg(args: list[str]) -> int | None:
    """Read ``--quiet-for SECONDS`` off the command line, or None if absent.

    Accepts a plain number of seconds and the ``30m`` / ``2h`` shorthands, because
    the question is always asked in minutes or hours and converting in your head at
    the moment you suspect an agent is stuck is how you get it wrong.
    """
    if "--quiet-for" not in args:
        return None
    index = args.index("--quiet-for")
    if index + 1 >= len(args):
        raise ClientError("usage: a2a-client agents --quiet-for <seconds|30m|2h>")
    raw = args[index + 1]
    units = {"s": 1, "m": 60, "h": 3600}
    factor = units.get(raw[-1:], 1) if raw[-1:] in units else 1
    number = raw[:-1] if raw[-1:] in units else raw
    try:
        value = int(number) * factor
    except ValueError:
        raise ClientError(
            f"--quiet-for expects seconds or 30m/2h; got {raw!r}"
        ) from None
    if value < 0:
        raise ClientError("--quiet-for cannot be negative")
    return value


#: Options each command accepts. Anything else typed with a leading ``--`` is a
#: mistake, and mistakes have to look like mistakes.
#:
#: Commands that take free text (``send``, ``introduce``, ``status``) are absent on
#: purpose: their trailing words are the message, and a body may legitimately begin a
#: line with ``--``. Refusing there would reject valid content, which is a worse
#: failure than the one being fixed.
KNOWN_FLAGS: dict[str, frozenset[str]] = {
    "agents": frozenset({"--json", "--quiet-for", "--retired"}),
    "inbox": frozenset({"--json"}),
    "read": frozenset(),
    "whoami": frozenset(),
    "retire": frozenset(),
}


def _unknown_flags(command: str, args: list[str]) -> list[str]:
    """Flags this command does not understand.

    Asking for something that does not exist must not be indistinguishable from
    asking for something that does. Measured by lexboe-113-ns3073844 on 2026-08-16:
    `agents --this-flag-does-not-exist` printed a perfectly normal 12-row listing and
    exited 0. The consequence is not limited to flags that have not shipped yet — a
    typo (`--retried`), or an option from a newer version than the one installed, both
    return a confident answer to a question that was never asked. Which is how an
    agent concluded "nobody is retired" from a hub holding three retired rows.
    """
    known = KNOWN_FLAGS.get(command)
    if known is None:
        return []
    return [a for a in args[1:] if a.startswith("--") and a not in known]


def main(argv: list[str] | None = None, client: HubClient | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)

    # `--session NAME` may appear anywhere; it overrides A2A_HUB_SESSION.
    session_override: str | None = None
    if "--session" in args:
        index = args.index("--session")
        if index + 1 >= len(args):
            print("usage: a2a-client --session <name> ...", file=sys.stderr)
            return 1
        session_override = args[index + 1]
        del args[index : index + 2]

    # Asking for help must never *do* anything. `a2a-client inbox --help` used to
    # ignore the flag and run the command: two agents on 2026-08-14 typed it while
    # reading up on sessions and were shown a mailbox that was not theirs (138 and
    # 122 tasks, both the shared row). Read-only and harmless that time, but "help"
    # is the one word a user types when they are unsure — the moment side effects
    # are least welcome.
    if not args or "-h" in args or "--help" in args:
        print(__doc__)
        # And, when help was asked *about a command*, what that command accepts.
        # Reported by glucoskin-ns3073844: `agents --help` printed the module
        # docstring, exactly like `inbox --help`, so there was no way to discover a
        # subcommand's options at all — you could not ask the tool, and typing a wrong
        # flag did not correct you either. Discovery by error message is not
        # discovery.
        if args and args[0] in KNOWN_FLAGS:
            accepted = " ".join(sorted(KNOWN_FLAGS[args[0]])) or "(no options)"
            print(f"options for {args[0]}: {accepted}")
        return 0

    unknown = _unknown_flags(args[0], args)
    if unknown:
        accepted = " ".join(sorted(KNOWN_FLAGS[args[0]])) or "(none)"
        print(
            f"a2a-client {args[0]}: unknown option {' '.join(unknown)}\n"
            f"accepted here: {accepted}\n"
            "Refusing rather than answering: an option this build does not know is "
            "either a typo or one from a newer version, and both would otherwise get "
            "a normal-looking answer to a question nobody asked.",
            file=sys.stderr,
        )
        return 2

    try:
        if client is None:
            config = ClientConfig.load()
            if session_override:
                # Naming the session on the command line *is* setting it for this
                # process, so it is not the host's shared default any more. Without
                # clearing the flag the warning fired on every `--session` call —
                # telling agents doing exactly the right thing that they were doing
                # the wrong thing, and inviting them to "fix" it by dropping the
                # flag, which drops them straight into the shared row.
                config = replace(
                    config,
                    session=session_override,
                    session_from_shared_file=False,
                )
            hub = HubClient(config)
        else:
            hub = client
        command = args[0]

        # A session inherited from the host-wide config file is not this agent's
        # identity, and it silently applies to EVERY command: `inbox` reads the
        # shared mailbox, `send` signs as the shared identity. Warn on all of them —
        # `introduce` refuses outright below, because it also overwrites other rows.
        if hub.config.session_from_shared_file and command != "whoami":
            # Now that the flag wins, this can only mean "you set no session at all".
            # So say that, rather than telling someone to do what they already did:
            # analog-brain-ns3073844 measured all three cases and pointed out that
            # the old wording made the correct usage and the dangerous one produce
            # the *same* line — which teaches the fleet to ignore both.
            print(
                f"warning: you have not set a session, so this is running as "
                f"{hub.config.identity!r} — the host-wide default that EVERY process "
                "here falls back to. You are reading and signing as a shared "
                "identity, not your own.\n"
                "Set A2A_HUB_SESSION (note the HUB) or pass --session <name>.",
                file=sys.stderr,
            )

        if command == "whoami":
            # Never print the token.
            print(f"identity : {hub.config.identity}")
            print(f"hub      : {hub.config.url}")

        elif command == "inbox":
            result = hub.list_all_tasks()
            if "--json" in args:
                print(json.dumps(result, indent=2))
            else:
                returned, total = len(result.get("tasks", [])), result.get(
                    "totalSize", 0
                )
                # Both numbers, always. Printing only the total is what let a
                # truncated read pass for a whole one for weeks.
                print(
                    f"mailbox of {hub.config.identity}: "
                    f"{returned} of {total} task(s), "
                    f"{result.get('pagesRead', 1)} page(s)"
                )
                if result.get("incomplete") or returned != total:
                    print(
                        f"WARNING: read {returned} of {total} — this is NOT your whole "
                        "mailbox. Older messages are missing."
                    )
                for task in result.get("tasks", []):
                    print(format_task(task))

        elif command == "introduce":
            if hub.config.session_from_shared_file:
                print(
                    f"refusing to register as {hub.config.identity!r}: that session "
                    "comes from the shared config file, not from this agent.\n"
                    "Every process on this host that does not set its own session "
                    "reads the same value, so registering under it overwrites "
                    "whoever registered last.\n"
                    "Set A2A_HUB_SESSION (note the HUB), or pass --session <name>.",
                    file=sys.stderr,
                )
                return 1
            # host is taken from the machine, not typed: one less thing to get wrong,
            # and a wrong host in a register is worse than no host.
            if len(args) < 3:
                raise ClientError(
                    "usage: a2a-client introduce <role> <project[,project...]> "
                    "<what you are doing...>"
                )
            result = hub.introduce(
                role=args[1],
                host=socket.gethostname(),
                projects=[p for p in args[2].split(",") if p],
                status=" ".join(args[3:]),
            )
            print(f"introduced {result['identity']} as {result['role']}")
            for warning in result.get("warnings", []):
                print(f"warning: {warning}", file=sys.stderr)
            if not _appears_in_register(hub, result["identity"]):
                # Suggested by quantlab-signal-ns3073844 after losing half an hour to
                # exactly this: a declaration that does not take is indistinguishable
                # from one that does, so the only detection available was an external
                # check someone had to remember to run. A rule that depends on memory
                # is a rule that fails.
                print(
                    f"ERROR: {result['identity']} accepted the introduction and does "
                    "NOT appear in the register, so nobody can see you.\n"
                    "Most likely this identity was retired: re-declaring does not undo "
                    "a retirement on a hub older than this fix.\n"
                    "Introduce yourself under a session name that was never retired.",
                    file=sys.stderr,
                )
                return 1

        elif command == "status":
            if len(args) < 2:
                raise ClientError("usage: a2a-client status <what you are doing...>")
            result = hub.set_status(" ".join(args[1:]))
            print(f"status of {result['identity']}: {result['status']}")

        elif command == "retire":
            result = hub.retire()
            print(f"retired {result['identity']}")

        elif command == "agents":
            quiet_for = _quiet_for_arg(args)
            result = hub.agents(quiet_for, retired="--retired" in args)
            if "--json" in args:
                print(json.dumps(result, indent=2))
            else:
                listed = result.get("agents", [])
                if quiet_for is not None and not listed:
                    # A silent "nothing" reads like a failed command. Say it plainly:
                    # nobody being quiet is an answer, and a useful one.
                    print(f"nobody has been quiet for {quiet_for}s")
                for agent in listed:
                    print(format_agent(agent))

        elif command == "read":
            if len(args) < 2:
                raise ClientError("usage: a2a-client read <task-id>")
            print(json.dumps(hub.get_task(args[1]), indent=2))

        elif command == "send":
            # --body-file exists because the alternative has already cost us. Text
            # passed as an argument goes through the shell, and message bodies here
            # routinely contain example commands: on 2026-08-10 a runbook posted this
            # way had its own examples EXECUTED, restarting shared infrastructure with
            # no announcement. lexboe-116-ns3073844 hit the milder version three times
            # in two days — backticks silently eaten, once replaced by the output of
            # `df`. They had written the rule "long bodies go by --body-file" and could
            # only apply it to GitHub, because here the flag did not exist.
            if len(args) >= 4 and args[2] == "--body-file":
                recipient = args[1]
                try:
                    text = pathlib.Path(args[3]).read_text(encoding="utf-8")
                except OSError as error:
                    raise ClientError(f"cannot read {args[3]}: {error}") from error
            elif len(args) >= 3:
                recipient = args[1]
                text = " ".join(args[2:])
            else:
                raise ClientError(
                    "usage: a2a-client send <recipient> <text...>\n"
                    "       a2a-client send <recipient> --body-file <path>"
                )
            result = hub.send_message(recipient, text)
            task = result.get("task", {})
            state = task.get("status", {}).get("state", "?").replace(
                "TASK_STATE_", ""
            )
            print(f"-> {recipient}: {state} (task {task.get('id', '?')[:8]})")

        else:
            raise ClientError(f"unknown command: {command}")

    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0
