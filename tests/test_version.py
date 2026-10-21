"""The package version has exactly one source of truth.

``__version__`` used to be a hand-maintained string in ``a2a_hub/__init__.py``, copied
from ``pyproject.toml`` and never updated when the latter bumped. Measured live on
2026-10-21: ``__init__.py`` said ``"0.1.0"`` while ``pyproject.toml`` said ``"0.2.0"`` —
the hub's own Agent Card had been serving the wrong version, with nothing to notice.

The fix removes the second copy instead of adding a check on top of it. The test below
is the regression guard: it is what turns a reintroduced duplicate into a failing gate
instead of a silent one.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import a2a_hub

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_version() -> str:
    return tomllib.loads(_PYPROJECT.read_text())["project"]["version"]


def versions_match(a: str, b: str) -> bool:
    """Exact match only.

    A loose comparison (stripped, case-folded, trailing-zero-tolerant...) would call two
    different versions "close enough", which is exactly the failure mode this guard
    exists to rule out.
    """
    return a == b


def test_installed_version_matches_pyproject():
    """The real regression guard: fails the gate the moment the two drift again."""
    assert versions_match(a2a_hub.__version__, _pyproject_version())


def test_versions_match_rejects_a_real_divergence():
    """Triggers: two genuinely different versions must not be called a match."""
    assert not versions_match("0.1.0", "0.2.0")


def test_versions_match_accepts_identical_strings():
    """Does not trigger: two identical versions must be called a match."""
    assert versions_match("0.2.0", "0.2.0")


def test_versions_match_rejects_near_misses_that_look_equal():
    """Near-misses: close enough to pass a careless check, and must not."""
    assert not versions_match("0.2.0", "0.2.0 ")  # trailing whitespace
    assert not versions_match("0.2.0", "v0.2.0")  # "v" prefix
    assert not versions_match("0.2.0", "0.2")  # dropped patch component
    assert not versions_match("0.2.0", "0.2.0.0")  # extra component
    assert not versions_match("0.2.0", "0.2.0-dev")  # suffix
