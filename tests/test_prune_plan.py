"""The prune rules are refusals, and a refusal that is not tested is a hope.

The workflow that uses this deletes package versions, which is irreversible, so each rule
that protects something gets a case here.
"""

from __future__ import annotations

import datetime
import importlib.util
import pathlib

import pytest

SPEC = importlib.util.spec_from_file_location(
    "prune_plan",
    pathlib.Path(__file__).resolve().parents[1] / ".github" / "scripts" / "prune_plan.py",
)
prune_plan = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(prune_plan)

NOW = datetime.datetime(2026, 8, 11, 12, 0, tzinfo=datetime.timezone.utc)
PINNED = "sha256:fa4a72ef"


def version(vid, tags, days_old, digest="sha256:0000"):
    created = NOW - datetime.timedelta(days=days_old)
    return {
        "id": vid,
        "name": digest,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": {"container": {"tags": tags}},
    }


def classify(versions, grace=30, live_trees=frozenset(), pinned=PINNED):
    return prune_plan.classify(versions, grace, NOW, set(live_trees), pinned)


def reason_for(keep, vid):
    return next(reason for kid, _, _, reason in keep if kid == vid)


def test_untagged_versions_are_never_candidates():
    """They are indistinguishable from the running image's own children."""
    delete, keep = classify([version(1, [], days_old=400)])
    assert delete == []
    assert "untagged" in reason_for(keep, 1)


def test_the_pinned_digest_is_never_a_candidate_even_without_tags():
    """The deployment references the image by digest, so the digest is the real reference."""
    delete, keep = classify([version(1, [], days_old=400, digest=PINNED)])
    assert delete == []
    assert "pinned" in reason_for(keep, 1)


def test_promoted_versions_are_never_candidates():
    old = version(1, ["sha-abc", "tree-def", "latest"], days_old=400)
    delete, keep = classify([old])
    assert delete == []
    assert "promoted" in reason_for(keep, 1)


def test_a_tree_of_an_open_pull_request_survives_any_age():
    """Age is a proxy for 'unreferenced'; an open PR is the reference itself."""
    stale = version(1, ["tree-c24b0ed"], days_old=400)
    delete, keep = classify([stale], live_trees={"c24b0ed"})
    assert delete == []
    assert "still referenced" in reason_for(keep, 1)


def test_an_unreferenced_tree_past_the_grace_period_is_deleted():
    delete, _ = classify([version(1, ["tree-48cd8c0"], days_old=400)])
    assert [vid for vid, *_ in delete] == [1]


def test_an_unreferenced_tree_inside_the_grace_period_survives():
    delete, keep = classify([version(1, ["tree-48cd8c0"], days_old=3)])
    assert delete == []
    assert "grace period" in reason_for(keep, 1)


def test_an_unrecognised_tag_scheme_is_kept():
    """Unknown means keep: a rule that has not seen a shape must not guess."""
    delete, keep = classify([version(1, ["v1.2.3"], days_old=400)])
    assert delete == []
    assert "unrecognised" in reason_for(keep, 1)


def test_paginated_listings_are_flattened():
    """`gh api --paginate --slurp` yields one array per page, not one flat array."""
    pages = [[version(1, ["sha-a"], 1)], [version(2, ["tree-b"], 400)]]
    assert [v["id"] for v in prune_plan.flatten(pages)] == [1, 2]


def test_a_flat_listing_is_left_alone():
    flat = [version(1, ["sha-a"], 1)]
    assert prune_plan.flatten(flat) == flat


def test_an_empty_listing_does_not_crash_the_flattener():
    assert prune_plan.flatten([]) == []


def test_main_refuses_when_the_pinned_digest_is_absent(tmp_path, capsys):
    """A listing without the deployed digest is not the registry we think it is."""
    versions = tmp_path / "versions.json"
    versions.write_text('[{"id":1,"name":"sha256:other","created_at":"2026-08-01T00:00:00Z",'
                        '"metadata":{"container":{"tags":["sha-abc"]}}}]')
    trees = tmp_path / "trees.txt"
    trees.write_text("")
    argv = [str(versions), "30", "sha-abc", str(trees), PINNED]
    assert prune_plan.main(argv) == 1
    assert "Refusing to act" in capsys.readouterr().err


def test_main_refuses_when_the_cut_build_is_absent(tmp_path, capsys):
    versions = tmp_path / "versions.json"
    versions.write_text(f'[{{"id":1,"name":"{PINNED}","created_at":"2026-08-01T00:00:00Z",'
                        '"metadata":{"container":{"tags":["sha-abc"]}}}]')
    trees = tmp_path / "trees.txt"
    trees.write_text("")
    argv = [str(versions), "30", "sha-missing", str(trees), PINNED]
    assert prune_plan.main(argv) == 1
    assert "Refusing to act" in capsys.readouterr().err


def test_main_stops_when_the_live_tree_list_is_unreadable(tmp_path):
    """Not knowing what is referenced must stop the run, never widen the delete set."""
    versions = tmp_path / "versions.json"
    versions.write_text("[]")
    argv = [str(versions), "30", "sha-abc", str(tmp_path / "missing.txt"), PINNED]
    with pytest.raises(FileNotFoundError):
        prune_plan.main(argv)
