"""Extension negotiation: the `A2A-Extensions` header, honoured and echoed.

#48 item 3, the half the issue measured as a cheap conformance win that keeps the
feature. The register was already declared the way the spec asks — an entry in
`AgentCard.capabilities.extensions`, `required=false` — but the negotiation half was
missing entirely: `grep -rn 'a2a-extensions' src/` returned nothing, so the routes
answered whether or not the caller had said it speaks the extension. Declared but
never negotiated is a capability a client cannot confirm it is using.

What the spec asks for, and what this does:

- a client **activates** an extension by naming its URI in the `A2A-Extensions`
  request header;
- the server **echoes** the ones it actually activated in the response header, so the
  client learns what it got rather than assuming;
- URIs the server does not implement are **ignored, not echoed and not an error** —
  that is what makes the echo informative. A server that echoed the request back
  would let a client believe an extension it invented is active.

What this deliberately does **not** do: require the header. The extension is declared
`required=false`, and eleven live clients send nothing of the sort — measured. An
optional extension that starts refusing callers who ignore it was never optional, and
the point of `required=false` is that ignoring it keeps working.
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.requests import Request
from starlette.responses import Response


#: Request and response header, spelled as the specification spells it. Matching is
#: case-insensitive in both directions because HTTP headers are.
A2A_EXTENSIONS_HEADER = "A2A-Extensions"


def activated(request: Request, supported: Iterable[str]) -> list[str]:
    """The extension URIs this caller asked for **and** this server implements.

    Order follows the request, and duplicates collapse: the value is echoed back, and
    a response that reordered or repeated what was asked for would be harder to
    compare against what was sent than one that did not.
    """
    known = set(supported)
    raw = request.headers.get(A2A_EXTENSIONS_HEADER, "")
    active: list[str] = []
    for uri in (part.strip() for part in raw.split(",")):
        if uri in known and uri not in active:
            active.append(uri)
    return active


def echoing(handler, supported: Iterable[str]):
    """Wrap a route so it reports which extensions the request activated.

    A wrapper rather than a line in each handler: the register has four routes and
    every future one would have to remember, which is the shape of a rule that holds
    until someone adds a fifth. Nothing about the response body changes — activation
    is a fact about the request, so it belongs in a header and not in the payload.
    """
    known = tuple(supported)

    async def wrapped(request: Request) -> Response:
        response = await handler(request)
        active = activated(request, known)
        if active:
            response.headers[A2A_EXTENSIONS_HEADER] = ", ".join(active)
        return response

    # Keep the handler's name so tracebacks and route introspection still say which
    # route this is, rather than reporting four routes all called `wrapped`.
    wrapped.__name__ = getattr(handler, "__name__", "wrapped")
    wrapped.__doc__ = handler.__doc__
    return wrapped
