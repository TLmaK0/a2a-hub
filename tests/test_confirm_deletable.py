"""The last check before an irreversible call, so every way it can say no gets a case.

The plan is drawn once and the deletions happen afterwards. These tests describe what may
have changed in between, and each one must stop the run rather than delete.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

SPEC = importlib.util.spec_from_file_location(
    "confirm_deletable",
    pathlib.Path(__file__).resolve().parents[1] / ".github" / "scripts" / "confirm_deletable.py",
)
confirm = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(confirm)

PINNED = "sha256:fa4a72ef"


def check(tmp_path, tags, live_trees=(), digest="sha256:0000"):
    version = tmp_path / "version.json"
    version.write_text(json.dumps(
        {"id": 42, "name": digest, "metadata": {"container": {"tags": list(tags)}}}
    ))
    trees = tmp_path / "trees.txt"
    trees.write_text("\n".join(live_trees))
    return confirm.main([str(version), str(trees), PINNED])


def test_a_still_unreferenced_tree_is_confirmed(tmp_path):
    assert check(tmp_path, ["tree-48cd8c0"]) == 0


def test_a_pr_opened_since_the_plan_stops_the_run(tmp_path, capsys):
    """The race this whole check exists for: referenced between planning and deleting."""
    assert check(tmp_path, ["tree-48cd8c0"], live_trees=["48cd8c0"]) == 1
    assert "referenced by an open pull request" in capsys.readouterr().err


def test_promotion_since_the_plan_stops_the_run(tmp_path, capsys):
    assert check(tmp_path, ["tree-48cd8c0", "sha-abc", "latest"]) == 1
    assert "promoted since the plan was drawn" in capsys.readouterr().err


def test_becoming_the_pinned_digest_stops_the_run(tmp_path, capsys):
    assert check(tmp_path, ["tree-48cd8c0"], digest=PINNED) == 1
    assert "pinned digest" in capsys.readouterr().err


def test_losing_its_tags_stops_the_run(tmp_path, capsys):
    """An untagged version is a child of some index, never litter to collect."""
    assert check(tmp_path, []) == 1
    assert "no tags" in capsys.readouterr().err


def test_an_unrecognised_tag_stops_the_run(tmp_path, capsys):
    assert check(tmp_path, ["tree-48cd8c0", "v1.2.3"]) == 1
    assert "unrecognised tags" in capsys.readouterr().err
