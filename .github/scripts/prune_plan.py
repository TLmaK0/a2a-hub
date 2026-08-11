"""Decide which container versions may be deleted. Prints a plan; deletes nothing.

Kept out of the workflow YAML on purpose: a heredoc inside a `run:` block is indented,
which both breaks the heredoc terminator and feeds indented source to Python. As a file it
is also testable on real data before it ever runs in CI.

Usage: prune_plan.py <versions.json> <grace_days> <cut_tag> <live_trees_file> <pinned_digest>

`live_trees_file` holds one git tree hash per line: the trees of every OPEN pull request.
A `tree-*` tag *is* a tree hash, so this answers "does anything still point at this image?"
directly, instead of using age as a proxy for it.

Exit 1 if the agreed cut build is absent, or if the pinned digest is not in the listing —
refuse rather than reason from a bad map.
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


def classify(
    versions: list[dict],
    grace: int,
    now: datetime.datetime,
    live_trees: set[str],
    pinned_digest: str,
):
    """Return (delete, keep). Every keep carries the reason it is untouchable."""
    delete: list[tuple] = []
    keep: list[tuple] = []
    for version in versions:
        current = tags(version)
        age = age_days(version, now)
        if version.get("name") == pinned_digest:
            # Since the deployment pins by digest, this is the reference that actually
            # matters; the sha-* tag below is only a proxy for it.
            reason = "pinned: this is the digest the promoted branch publishes for deployment"
        elif not current:
            reason = "untagged: a child of some index, indistinguishable from the running image's own children"
        elif any(t == "latest" or t.startswith("sha-") for t in current):
            reason = "promoted: reachable from main, and anything deployed necessarily carries a sha-* tag"
        elif all(t.startswith("tree-") for t in current):
            referenced = sorted(t for t in current if t[len("tree-"):] in live_trees)
            if referenced:
                reason = f"still referenced: {referenced[0]} is the tree of an open pull request"
            elif age >= grace:
                delete.append(
                    (version["id"], current, age, "tree-* only, unreferenced, past the grace period")
                )
                continue
            else:
                reason = f"tree-* only but {age}d old, inside the {grace}d grace period"
        else:
            reason = "unrecognised tag scheme: unknown means keep"
        keep.append((version["id"], current, age, reason))
    return delete, keep


def main(argv: list[str] | None = None) -> int:
    path, grace_s, cut_tag, trees_path, pinned_digest = (
        sys.argv[1:6] if argv is None else argv[:5]
    )
    grace = int(grace_s)
    versions = json.load(open(path))
    now = datetime.datetime.now(datetime.timezone.utc)

    # No list of live trees is not "nothing is referenced", it is "we do not know".
    # An unreadable file must stop the run, not widen the delete set.
    with open(trees_path) as fh:
        live_trees = {line.strip() for line in fh if line.strip()}

    # The cut is a TAG lookup, never a timestamp comparison: the untagged children of the
    # cut build were created one second BEFORE its index, so a timestamp rule would delete
    # parts of the image that must survive.
    cut = next((v for v in versions if cut_tag in tags(v)), None)
    pinned = next((v for v in versions if v.get("name") == pinned_digest), None)

    delete, keep = classify(versions, grace, now, live_trees, pinned_digest)

    print(f"cut={cut_tag} cut_version={'found' if cut else 'MISSING'}")
    print(f"pinned={pinned_digest} pinned_version={'found' if pinned else 'MISSING'}")
    print(f"live_trees={len(live_trees)} (open pull requests)")
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
    if pinned is None:
        # The digest that is deployed must be present and accounted for. If it is not in
        # the listing, the listing is not the registry we think it is.
        print(
            "::error::The digest published for deployment is not in this listing. Refusing to act.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
