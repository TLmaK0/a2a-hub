#!/usr/bin/env bash
# Record the digest just promoted, on a dedicated orphan branch, so anyone can read which
# image is the current validated one WITHOUT credentials.
#
# Why: the deployment repo cannot read this package's versions — it is private and linked to
# a different repository, so its token is refused. This repo, however, is public, so what it
# writes into itself is world-readable. That removes a credential from the deploy chain
# entirely.
#
# Why an orphan branch and not `main`: adding a file to main would change main's TREE, and
# the tree is exactly what identifies the validated image (`tree-<sha>`). Recording the fact
# on main would therefore invalidate the thing being recorded. The branch also gives an
# auditable history of promotions, and CI here only runs on main and pull requests, so
# pushing to it triggers nothing.
#
# Usage: record_promoted.sh <branch> <commit_sha> <digest>
set -euo pipefail

branch="$1"
commit="$2"
digest="$3"

case "$digest" in
  sha256:*) ;;
  *) echo "::error::refusing to record something that is not a digest: ${digest}" >&2; exit 1 ;;
esac

# Work in a scratch clone so the job's checkout is never touched.
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

git -C "${work}" init -q
git -C "${work}" remote add origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
git -C "${work}" config user.name "github-actions[bot]"
git -C "${work}" config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# Continue the branch's history if it exists; start it if this is the first promotion.
if git -C "${work}" fetch -q --depth 1 origin "${branch}" 2>/dev/null; then
  git -C "${work}" checkout -q -B "${branch}" FETCH_HEAD
else
  git -C "${work}" checkout -q --orphan "${branch}"
fi

printf '{\n  "sha": "%s",\n  "digest": "%s",\n  "at": "%s"\n}\n' \
  "${commit}" "${digest}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${work}/promoted.json"

git -C "${work}" add promoted.json
if git -C "${work}" diff --cached --quiet; then
  echo "promoted.json unchanged; nothing to record."
  exit 0
fi

git -C "${work}" commit -q -m "chore(promote): ${commit} -> ${digest}"
git -C "${work}" push -q origin "HEAD:${branch}"
echo "Recorded on branch ${branch}: ${commit} -> ${digest}"
