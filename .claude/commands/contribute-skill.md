Open a pull request against the upstream skills repo with your local changes to a named skill file.

Usage: `/contribute-skill <skill-name>`

Example: `/contribute-skill groom-ticket`

## Steps

1. Read `.claude/settings.json` and extract `skills_source` and `skills_version`.

2. Parse `skills_source` to get `<owner>/<repo>`.

3. Resolve the local skill file path: `.claude/commands/<skill-name>.md`. Confirm it exists.

4. Fetch the upstream base version of the file at `skills_version`:
   ```bash
   curl -s "https://raw.githubusercontent.com/<owner>/<repo>/<skills_version>/.claude/commands/<skill-name>.md" -o /tmp/upstream-<skill-name>.md
   ```

5. Show a diff between upstream and local:
   ```bash
   diff /tmp/upstream-<skill-name>.md .claude/commands/<skill-name>.md
   ```
   If no diff, print: `No changes to contribute for <skill-name>.` and stop.

6. Clone the skills repo to a temp directory, create a branch, apply the diff, and push:
   ```bash
   git clone "https://github.com/<owner>/<repo>.git" /tmp/contribute-skill-work
   cd /tmp/contribute-skill-work
   git checkout -b "contribute/<skill-name>-from-$(basename $(git rev-parse --show-toplevel))"
   cp /path/to/project/.claude/commands/<skill-name>.md .claude/commands/<skill-name>.md
   git add .claude/commands/<skill-name>.md
   git commit -m "Contribute updated <skill-name> skill"
   git push -u origin HEAD
   ```

7. Open a pull request using `gh`:
   ```bash
   gh pr create \
     --repo "<owner>/<repo>" \
     --title "Contribute <skill-name> from <project-name>" \
     --body "Local modifications to \`<skill-name>.md\` contributed from \`<project-name>\` at \`<skills_version>\`." \
     --head "contribute/<skill-name>-from-<project-name>"
   ```

8. Print the PR URL. Clean up `/tmp/contribute-skill-work` and `/tmp/upstream-<skill-name>.md`.

## Notes

- Requires `gh` CLI authenticated to GitHub.
- The PR targets `main` on `<owner>/<repo>`. The upstream maintainer reviews and merges.
- After your PR is merged and a new release tag is published, run `/update-skills` to pull it back.
- Contributes only the single named skill — not bulk changes to all skills.
