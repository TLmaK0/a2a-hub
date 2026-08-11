"""Re-check ONE version against fresh state, immediately before deleting it.

The plan is computed once; the deletions happen afterwards, one API call at a time. In
between, a PR can open, a build can publish, a merge can promote. A version that was litter
when the plan was drawn may be referenced by the time its turn comes, and deleting package
versions is irreversible.

So every deletion re-reads the version and the live trees at the moment of deleting, rather
than trusting the list. Anything unexpected — new tags, a tag that now matches a live tree,
the pinned digest, a version that no longer looks like litter — stops the run.

Usage: confirm_deletable.py <version.json> <live_trees_file> <pinned_digest>
Exit 0 only if this version is still safe to delete.
"""

from __future__ import annotations

import json
import sys


def refuse(message: str) -> int:
    print(f"::error::{message} Stopping; nothing further is attempted.", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    version_path, trees_path, pinned_digest = (sys.argv[1:4] if argv is None else argv[:3])

    version = json.load(open(version_path))
    with open(trees_path) as fh:
        live_trees = {line.strip() for line in fh if line.strip()}

    vid = version.get("id")
    current = version.get("metadata", {}).get("container", {}).get("tags", []) or []

    if version.get("name") == pinned_digest:
        return refuse(f"Version {vid} is now the pinned digest.")
    if not current:
        return refuse(f"Version {vid} now has no tags, so it is indistinguishable from a child.")
    if any(t == "latest" or t.startswith("sha-") for t in current):
        return refuse(f"Version {vid} has been promoted since the plan was drawn: {current}.")
    if not all(t.startswith("tree-") for t in current):
        return refuse(f"Version {vid} carries unrecognised tags now: {current}.")
    referenced = [t for t in current if t[len("tree-"):] in live_trees]
    if referenced:
        return refuse(f"Version {vid} is now referenced by an open pull request: {referenced}.")

    print(f"confirmed {vid} still deletable: {','.join(current)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
