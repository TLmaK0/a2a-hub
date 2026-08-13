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
    a2a-client introduce <role> <project[,project...]> <what you are doing...>
    a2a-client status <what you are doing...>
    a2a-client retire
    a2a-client agents [--json]
    a2a-client [--session NAME] ...      # overrides A2A_HUB_SESSION
"""

from __future__ import annotations

import json
import os
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

    def agents(self) -> dict[str, Any]:
        """Who else is connected, what they are, and when they were last seen."""
        return self._http("/agents", None)

    def list_tasks(self, include_artifacts: bool = True) -> dict[str, Any]:
        """List the tasks waiting in *my* mailbox."""
        return self._rpc("ListTasks", {"includeArtifacts": include_artifacts})

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
    return (
        f"[{seen:>9}]{silent} {agent['identity']} {agent.get('role')} "
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

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    try:
        if client is None:
            config = ClientConfig.load()
            if session_override:
                config = replace(config, session=session_override)
            hub = HubClient(config)
        else:
            hub = client
        command = args[0]

        # A session inherited from the host-wide config file is not this agent's
        # identity, and it silently applies to EVERY command: `inbox` reads the
        # shared mailbox, `send` signs as the shared identity. Warn on all of them —
        # `introduce` refuses outright below, because it also overwrites other rows.
        if hub.config.session_from_shared_file and command != "whoami":
            print(
                f"warning: session {hub.config.session!r} comes from the shared config "
                "file, so this is NOT this agent's identity — every process on the "
                "host that does not set its own reads the same value. "
                "Set A2A_HUB_SESSION (note the HUB) or pass --session <name>.",
                file=sys.stderr,
            )

        if command == "whoami":
            # Never print the token.
            print(f"identity : {hub.config.identity}")
            print(f"hub      : {hub.config.url}")

        elif command == "inbox":
            result = hub.list_tasks()
            if "--json" in args:
                print(json.dumps(result, indent=2))
            else:
                print(
                    f"mailbox of {hub.config.identity}: "
                    f"{result.get('totalSize', 0)} task(s)"
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

        elif command == "status":
            if len(args) < 2:
                raise ClientError("usage: a2a-client status <what you are doing...>")
            result = hub.set_status(" ".join(args[1:]))
            print(f"status of {result['identity']}: {result['status']}")

        elif command == "retire":
            result = hub.retire()
            print(f"retired {result['identity']}")

        elif command == "agents":
            result = hub.agents()
            if "--json" in args:
                print(json.dumps(result, indent=2))
            else:
                for agent in result.get("agents", []):
                    print(format_agent(agent))

        elif command == "read":
            if len(args) < 2:
                raise ClientError("usage: a2a-client read <task-id>")
            print(json.dumps(hub.get_task(args[1]), indent=2))

        elif command == "send":
            if len(args) < 3:
                raise ClientError("usage: a2a-client send <recipient> <text...>")
            recipient = args[1]
            result = hub.send_message(recipient, " ".join(args[2:]))
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
