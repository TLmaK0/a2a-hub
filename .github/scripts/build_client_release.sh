#!/usr/bin/env bash
# Build the release artifacts for `a2a-client` and PROVE they run before publishing.
#
# Why this exists (issue #35): the client on a host was a symlink into this repo's editable
# virtualenv, so every agent ran whatever the shared main checkout happened to be at. The
# server deploy did not touch it, and the skew was silent — the flag simply "did not exist".
# Twice in a row a window shipped client fixes that reached nobody until someone remembered
# to fast-forward a checkout by hand.
#
# The client has nothing to do with the server deploy, so it is not deployed: it is BUILT and
# PUBLISHED as a versioned release, and a host installs a version. That is the whole change.
#
# This is a script and not inline `run:` steps on purpose. A workflow whose logic lives in
# YAML is only ever exercised by the workflow, and this repo has already shipped a gate that
# validated the YAML around code that could not run. Everything below executes the same way
# on a laptop as in CI.
#
# Usage: build_client_release.sh <tag>          e.g. build_client_release.sh v0.2.0
set -euo pipefail

tag="${1:?usage: build_client_release.sh <tag>   (e.g. v0.2.0)}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

fail() { echo "::error::$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. The tag must be the version that gets packaged.
# ---------------------------------------------------------------------------
# A release tagged v0.2.0 whose wheel reports 0.1.0 is precisely the silent skew #35 is
# about, moved one level up: the host would install "0.2.0" and run something else, and
# nothing would say so. Checked before anything is built, so a mismatch costs a second.
case "${tag}" in
  v*) tag_version="${tag#v}" ;;
  *)  fail "tag must look like v<version>, got '${tag}'" ;;
esac

project_version="$(
  python3 - <<'PY'
import re, pathlib
text = pathlib.Path("pyproject.toml").read_text()
# The [project] version, which is the first bare `version =` in the file; dependency
# pins live inside lists and are quoted differently, so they cannot match this.
match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
print(match.group(1) if match else "")
PY
)"

[ -n "${project_version}" ] || fail "could not read version from pyproject.toml"
if [ "${tag_version}" != "${project_version}" ]; then
  fail "tag ${tag} says ${tag_version} but pyproject.toml says ${project_version}. Bump the
version in pyproject.toml in a commit and tag that commit — the tag must not be the only
place the version exists, or the artifact and its name disagree."
fi
echo "version: ${project_version} (tag ${tag})"

# ---------------------------------------------------------------------------
# 2. The lock must match its source before anything is built from it.
# ---------------------------------------------------------------------------
# `uv build` resolves from pyproject.toml, so a stale lock does not break the build — it
# breaks the claim that what ships is what the gate tested. `--check` never rewrites; a bare
# `uv` command would repair the file and hide the problem it was asked about.
uv lock --check

# ---------------------------------------------------------------------------
# 3. Build the artifacts.
# ---------------------------------------------------------------------------
rm -rf dist
uv build --sdist --wheel
wheel="$(ls dist/*.whl)"
sdist="$(ls dist/*.tar.gz)"
[ -f "${wheel}" ] || fail "no wheel in dist/"
[ -f "${sdist}" ] || fail "no sdist in dist/"

# The filename carries the version, and a wheel named for a different version than the one
# just checked would mean the build backend read something else.
case "$(basename "${wheel}")" in
  *"-${project_version}-"*) ;;
  *) fail "wheel $(basename "${wheel}") is not version ${project_version}" ;;
esac

# ---------------------------------------------------------------------------
# 4. Install the built wheel somewhere clean and RUN it.
# ---------------------------------------------------------------------------
# This is the step that makes the release worth trusting. A wheel that imports is not a
# wheel whose console script exists: `a2a-client` is an entry point, and #35 happened
# because a working server said nothing about a client that could not do the thing. So the
# artifact is installed away from this checkout — no editable path, no `src/` on sys.path —
# and the actual command is executed.
probe="$(mktemp -d)"
trap 'rm -rf "${probe}"' EXIT
uv venv --quiet "${probe}/venv"
VIRTUAL_ENV="${probe}/venv" uv pip install --quiet "${wheel}"
client="${probe}/venv/bin/a2a-client"
[ -x "${client}" ] || fail "the wheel installed without an executable a2a-client"

# `--help` needs no hub, no token and no network, so it is the one invocation that is a
# statement about the artifact rather than about the environment. It also fails loudly if a
# module the CLI imports did not make it into the wheel, which is the realistic packaging
# defect: `uv_build` ships `src/a2a_hub/`, and a new module outside it would be missing here
# and nowhere else.
help_out="$("${client}" --help 2>&1)" || fail "a2a-client --help failed from the built wheel"
for expected in "a2a-client inbox" "a2a-client send" "a2a-client introduce"; do
  case "${help_out}" in
    *"${expected}"*) ;;
    *) fail "the built client does not offer '${expected}' — the wheel is not the client" ;;
  esac
done

# And it must refuse to run without configuration rather than crash: a release that
# tracebacks on a fresh host is indistinguishable from a broken install.
set +e
env -u A2A_HUB_URL -u A2A_HUB_TOKEN -u A2A_HUB_AGENT -u A2A_HUB_SESSION \
  HOME="${probe}" "${client}" whoami >"${probe}/out" 2>&1
status=$?
set -e
if grep -q "Traceback" "${probe}/out"; then
  fail "unconfigured a2a-client tracebacks instead of explaining itself:
$(cat "${probe}/out")"
fi
echo "unconfigured whoami: exit ${status}, no traceback"

echo "release artifacts verified:"
ls -l dist/
