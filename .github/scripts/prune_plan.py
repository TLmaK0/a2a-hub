"""Decide which container versions may be deleted. Prints a plan; deletes nothing.

Kept out of the workflow YAML on purpose: a heredoc inside a `run:` block is indented,
which both breaks the heredoc terminator and feeds indented source to Python. As a file it
is also testable on real data before it ever runs in CI.

Usage: prune_plan.py <versions.json> <grace_days> <cut_tag>
Exit 1 if the agreed cut build is absent — refuse rather than reason from a bad map.
"""

from __future__ import annotations

import datetime
import json
import sys


def tags(version: dict) -> list[str]:
    return version.get("metadata", {}).get("container", {}).get("tags", []) or []


def age_days(version: dict, now: datetime.datetime) -> int:
    created = datetime.datetime.fromisoformat(version["created_at"].replace("Z", "+00:00"))
    return (now - created).days


def classify(versions: list[dict], grace: int, cut_tag: str, now: datetime.datetime):
    """Return (delete, keep). Every keep carries the reason it is untouchable."""
    delete: list[tuple] = []
    keep: list[tuple] = []
    for version in versions:
        current = tags(version)
        age = age_days(version, now)
        if not current:
            reason = "untagged: a child of some index, indistinguishable from the running image's own children"
        elif any(t == "latest" or t.startswith("sha-") for t in current):
            reason = "promoted: reachable from main, and anything deployed necessarily carries a sha-* tag"
        elif all(t.startswith("tree-") for t in current):
            if age >= grace:
                delete.append((version["id"], current, age, "tree-* only, past the grace period"))
                continue
            reason = f"tree-* only but {age}d old, inside the {grace}d grace period"
        else:
            reason = "unrecognised tag scheme: unknown means keep"
        keep.append((version["id"], current, age, reason))
    return delete, keep


def main() -> int:
    path, grace_s, cut_tag = sys.argv[1], sys.argv[2], sys.argv[3]
    grace = int(grace_s)
    versions = json.load(open(path))
    now = datetime.datetime.now(datetime.timezone.utc)

    # The cut is a TAG lookup, never a timestamp comparison: the untagged children of the
    # cut build were created one second BEFORE its index, so a timestamp rule would delete
    # parts of the image that must survive.
    cut = next((v for v in versions if cut_tag in tags(v)), None)

    delete, keep = classify(versions, grace, cut_tag, now)

    print(f"cut={cut_tag} cut_version={'found' if cut else 'MISSING'}")
    print(f"total={len(versions)} keep={len(keep)} delete={len(delete)}")
    print("--- DELETE ---")
    for vid, t, age, why in delete:
        print(f"{vid}\t{','.join(t)}\t{age}d\t{why}")
    print("--- KEEP ---")
    for vid, t, age, why in keep:
        print(f"{vid}\t{','.join(t) or '(untagged)'}\t{age}d\t{why}")

    if cut is None:
        print("::error::The agreed cut build is not in this registry. Refusing to act.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
