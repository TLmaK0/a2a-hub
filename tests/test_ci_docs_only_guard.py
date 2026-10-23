"""A documentation-only change must not build or promote an image — the owner's rule.

    "pues no debe existir el skip ci"

`[skip ci]` verified nothing: a commit carrying three `.md` and one `.py` would land on
main with no gate having run, green because nothing ran. The replacement is the `decide`
job in `.github/workflows/ci.yml`, which classifies the CHANGED FILES and lets the rest
of the pipeline skip the build and the promotion only when every one of them is
documentation.

These tests do not re-implement the condition. They **lift its decision block verbatim**
out of the workflow and run it under `bash` against lists of changed files, so what is
asserted here cannot drift from what ships — the arrangement lexboe's
`test_deploy_docs_only_guard.py` uses, and for the same reason.

The cases that matter are the ones that must fall towards RUNNING: a false "docs only"
lets code onto main with no gates, a false "has code" wastes a runner, and those prices
are not comparable.
"""

from __future__ import annotations

import pathlib
import subprocess
import textwrap

import pytest

_WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
_START = "# --- decision block ---"
_END = "# --- end decision block ---"


def _decision_block() -> str:
    """The workflow's own verdict, sliced by its markers and dedented.

    Sliced from the raw file rather than parsed as YAML (PyYAML is not a dependency of
    this project), from the start of the marker's LINE, then dedented — YAML strips the
    block scalar's indentation before bash ever sees it, so the heredoc terminator must
    end up at column 0 here exactly as it does in the runner.
    """
    text = _WORKFLOW.read_text()
    start = text.rindex("\n", 0, text.index(_START)) + 1
    return textwrap.dedent(text[start : text.index(_END) + len(_END)])


def _verdict(changed: list[str]) -> str:
    """Run the shipped block over ``changed`` and return ``true``/``false``."""
    script = f"CHANGED='{chr(10).join(changed)}'\n{_decision_block()}\necho $docs_only"
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return done.stdout.strip().splitlines()[-1]


def test_the_block_is_still_findable_in_the_workflow() -> None:
    """If the markers move, every case below would silently test nothing."""
    block = _decision_block()
    assert "docs_only" in block
    assert "_DOCS=" in block


# --- must NOT build or promote: the case the rule was written for ----------------


def test_a_documentation_only_change_is_docs_only() -> None:
    """The positive control: the exact shape `[skip ci]` was being written for."""
    assert _verdict(["AGENTS.md"]) == "true"


def test_several_documents_at_once_are_still_docs_only() -> None:
    assert _verdict(["AGENTS.md", "docs/windows.md", "CLAUDE.md"]) == "true"


def test_readme_alone_counts_as_documentation() -> None:
    """Named on purpose: the one entry that is not obviously right.

    `README.md` is documentation by any reading AND an input to the image — the
    Dockerfile copies it because pyproject's `readme` needs it. Under the literal rule
    a README-only merge builds nothing, so the running image keeps the previous README
    text until something else builds. The consequence is chosen, not discovered; it is
    the same trade lexboe documents on its own README.
    """
    assert _verdict(["README.md"]) == "true"


# --- MUST run everything: the hole `[skip ci]` left open -------------------------


def test_a_code_change_runs_everything() -> None:
    assert _verdict(["src/a2a_hub/server.py"]) == "false"


def test_documentation_mixed_with_code_runs_everything() -> None:
    """The exact hole being closed: three .md and one .py is a CODE change."""
    assert _verdict(["AGENTS.md", "docs/a.md", "docs/b.md", "src/a2a_hub/auth.py"]) == "false"


def test_a_markdown_lookalike_is_not_documentation() -> None:
    """The pattern anchors on the extension: merely containing `.md` is not enough."""
    assert _verdict(["README.md.j2"]) == "false"


def test_an_empty_change_set_runs_everything() -> None:
    """Fail towards running: an uncomputable diff arrives here as an empty list, and
    "no files changed" is indistinguishable from "I could not tell"."""
    assert _verdict([]) == "false"


@pytest.mark.parametrize(
    "path",
    ["pyproject.toml", "uv.lock", "Dockerfile", ".github/workflows/ci.yml", "tests/conftest.py"],
)
def test_every_input_the_image_is_built_from_runs_everything(path: str) -> None:
    """Including this workflow itself: the change that ships this rule builds."""
    assert _verdict([path]) == "false"
