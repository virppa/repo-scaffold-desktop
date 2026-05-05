#!/usr/bin/env bash
#
# Auto-contribute .claude/commands/ files to the upstream
# virppa/repo-scaffold-skills repository.
#
# Environment variables:
#   BEFORE            - Git SHA of the commit before this push (before)
#   AFTER             - Git SHA of the commit after this push (after)
#   GITHUB_WORKSPACE  - Path to the source repository root
#   SKILLS_REPO_PATH  - Path to the cloned skills repository root
#   GH_TOKEN          - GitHub PAT for the skills repository (used by gh CLI)
#   SOURCE_REPO       - "<owner>/<repo>" of the source repository
#   SOURCE_SHA        - Full commit SHA to reference in PR body
#
# Usage (as GitHub Actions step):
#   env:
#     BEFORE: ${{ github.event.before }}
#     AFTER: ${{ github.event.after }}
#     GITHUB_WORKSPACE: "$GITHUB_WORKSPACE"
#     SKILLS_REPO_PATH: "skills-repo"
#     GH_TOKEN: ${{ secrets.SKILLS_REPO_PAT }}
#     SOURCE_REPO: ${{ github.repository }}
#     SOURCE_SHA: ${{ github.sha }}
#   run: ./scripts/contribute_skills.sh
#
# Exit codes:
#   0 - Completed (with or without changes)
#
# Author: Local worker (WOR-135)
# Date: 2026-05-05

set -euo pipefail

# --- Detect changed files ---------------------------------------------------

if [ "$BEFORE" = "0000000000000000000000000000000000000000" ]; then
  # Initial branch push — treat all existing command files as new
  FILES=$(git ls-files '.claude/commands/')
else
  FILES=$(git diff --name-only "$BEFORE" "$AFTER" -- '.claude/commands/')
fi

if [ -z "$FILES" ]; then
  echo "No .claude/commands/ files changed — skipping contribution."
  exit 0
fi

echo "Changed files to contribute:"
echo "$FILES"

# --- Copy changed files, push branch, open PR --------------------------------

if [ -d "$SKILLS_REPO_PATH" ] && [ -d "$SKILLS_REPO_PATH/.git" ]; then
  cd "$SKILLS_REPO_PATH"

  git config user.name "github-actions[bot]"
  git config user.email "github-actions[bot]@users.noreply.github.com"

  BRANCH="auto-contribute/from-${SOURCE_SHA:0:8}"
  git checkout -b "$BRANCH"

while IFS= read -r FILE; do
  [ -z "$FILE" ] && continue
  SRC="${GITHUB_WORKSPACE}/${FILE}"
  if [ ! -f "$SRC" ]; then
    echo "Skipping (deleted or missing from source): $FILE"
    continue
  fi
  mkdir -p "$(dirname "$FILE")"
  cp "$SRC" "$FILE"
  git add "$FILE"
done <<< "$FILES"

if git diff --cached --quiet; then
  echo "No staged changes after copy — skipping PR creation."
  exit 0
fi

git commit -m "Auto-contribute .claude/commands/ from ${SOURCE_REPO}@${SOURCE_SHA:0:8}"
git push -u origin "$BRANCH"

python3 - <<PYEOF
import os

source_repo = os.environ["SOURCE_REPO"]
source_sha = os.environ["SOURCE_SHA"]
changed_files = os.environ["FILES"]

file_list = "\n".join(
    f"- `{f}`" for f in changed_files.strip().splitlines() if f
)

body = (
    f"Automated contribution from "
    f"[`{source_repo}`](https://github.com/{source_repo}/commit/{source_sha}).\n\n"
    f"Triggered by commit: `{source_sha}`\n\n"
    f"### Changed files\n\n{file_list}\n"
)

with open("/tmp/pr-body.md", "w") as fh:
    fh.write(body)
PYEOF

gh pr create \
  --repo virppa/repo-scaffold-skills \
  --title "Auto-contribute .claude/commands/ from ${SOURCE_REPO}" \
  --body-file /tmp/pr-body.md \
  --head "$BRANCH"
fi
