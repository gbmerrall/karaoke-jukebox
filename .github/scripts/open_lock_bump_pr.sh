#!/usr/bin/env bash
# Open (or refresh) a pull request for a uv.lock that the canary job upgraded.
#
# Usage: open_lock_bump_pr.sh <package> [<package> ...]
#
# Assumes: it runs in the canary job AFTER the upgraded lockfile has passed the
# tests, with GH_TOKEN set and `contents: write` + `pull-requests: write`.
# Exits 0 without doing anything when uv.lock is unchanged, or when the bump
# branch already carries exactly this lockfile.
set -euo pipefail

BRANCH="deps/canary-lock-bump"
BASE="main"

# Print the locked version of package $1 from a uv.lock read on stdin.
locked_version() {
  awk -v want="name = \"$1\"" '$0 == want { getline; gsub(/version = |"/, ""); print; exit }'
}

if git diff --quiet -- uv.lock; then
  echo "uv.lock unchanged: nothing to bump."
  exit 0
fi

title_parts=()
body_lines=""
for package in "$@"; do
  old_version="$(git show HEAD:uv.lock | locked_version "$package")"
  new_version="$(locked_version "$package" < uv.lock)"
  if [ "$old_version" != "$new_version" ]; then
    title_parts+=("$package to $new_version")
    body_lines+="- \`$package\`: $old_version -> $new_version"$'\n'
  fi
done

if [ "${#title_parts[@]}" -eq 0 ]; then
  title="deps: refresh uv.lock"
  body_lines="- Only transitive dependencies changed."$'\n'
else
  title="deps: bump $(IFS=,; echo "${title_parts[*]}" | sed 's/,/, /g')"
fi
echo "Lockfile changed: $title"

if git fetch --quiet origin "$BRANCH" 2>/dev/null && git diff --quiet FETCH_HEAD -- uv.lock; then
  echo "Branch $BRANCH already has this lockfile: leaving the existing PR alone."
  exit 0
fi

run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"
body="Opened by the yt-dlp canary workflow after the upgraded lockfile passed the live
download test, the fast test suite and lint.

${body_lines}
Transitive dependencies may have moved with them; see the diff.

Canary run: ${run_url}

Why this is not a Dependabot PR: Dependabot cannot update uv dependencies that are
declared with extras (dependabot/dependabot-core#16431).

CI does not start on pull requests opened by a workflow. The run linked above is
the evidence that this lockfile works."

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git checkout --quiet -B "$BRANCH"
git add uv.lock
git commit --quiet -m "$title"
git push --quiet --force origin "$BRANCH"

open_pr="$(gh pr list --head "$BRANCH" --base "$BASE" --state open --json number --jq '.[0].number // empty')"
if [ -n "$open_pr" ]; then
  gh pr edit "$open_pr" --title "$title" --body "$body"
  echo "Updated pull request #$open_pr."
else
  gh pr create --base "$BASE" --head "$BRANCH" --title "$title" --body "$body"
fi
