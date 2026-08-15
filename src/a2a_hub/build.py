"""What code is this, exactly — for the server and for the client.

The version in ``pyproject.toml`` cannot answer that: it is ``0.1.0`` on every
commit, so a client three days behind the hub reports the same version as the hub.
That is not hypothetical — on 2026-08-14 a window shipped three client-side fixes
and the fleet had none of them, because ``a2a-client`` is an editable install
pointing at a shared checkout that the deploy does not touch. Nothing said so; the
symptom was "that option does not exist".

**The identity used here is the git TREE, not the commit.** CI already tags the
image ``tree-<tree>`` and promotes that exact digest, precisely so that what runs
in production is bit-for-bit what passed the gate. The tree is what determines the
code; two commits with identical contents are the same artifact and should compare
equal. Using the commit sha instead would report a mismatch between a hub and a
client running byte-identical code.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


#: Set in the image at build time from the same tree hash CI tags the image with.
TREE_ENV = "A2A_HUB_TREE"

#: What to report when neither the environment nor a git checkout can answer.
UNKNOWN = "unknown"


def _git_tree(start: Path) -> str | None:
    """Tree hash of the checkout containing ``start``, or None if there is none.

    Deliberately tolerant: an installed copy with no git around it is a normal
    way to run this, not an error.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "HEAD^{tree}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tree = result.stdout.strip()
    return tree if result.returncode == 0 and tree else None


def revision() -> str:
    """Identity of the code this process is running.

    The environment wins: inside the image it is the tree CI built and tagged, and
    there is no git checkout in there to ask. Falling back to the source tree is
    what makes an editable install — which is how the fleet runs the client —
    report something meaningful instead of ``unknown``.
    """
    from_env = (os.environ.get(TREE_ENV) or "").strip()
    if from_env:
        return from_env
    return _git_tree(Path(__file__).resolve().parent) or UNKNOWN


def short(rev: str) -> str:
    """First 12 characters, which is what a human compares."""
    return rev[:12] if rev and rev != UNKNOWN else UNKNOWN
