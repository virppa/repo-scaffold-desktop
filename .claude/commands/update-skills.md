Fetch the latest `.claude/commands/` files from the upstream skills repo and update `skills_version` in `.claude/settings.json`.

## Steps

1. Read `.claude/settings.json` and extract `skills_source` and `skills_version`.

2. Parse `skills_source` (format: `github:<owner>/<repo>`) to get the owner and repo name.

3. Call the GitHub API to find the latest release tag:
   ```bash
   curl -s "https://api.github.com/repos/<owner>/<repo>/releases/latest" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])"
   ```

4. If already at the latest tag, print: `Skills are up to date (<version>).` and stop.

5. Otherwise, fetch the full tree for the latest tag:
   ```bash
   curl -s "https://api.github.com/repos/<owner>/<repo>/git/trees/<latest-tag>?recursive=1"
   ```
   Filter entries where `path` starts with `.claude/commands/` and `type == "blob"`.

6. For each file, download and overwrite the local copy:
   ```bash
   curl -s "https://raw.githubusercontent.com/<owner>/<repo>/<latest-tag>/<path>" -o "<path>"
   ```

7. Bump `skills_version` in `.claude/settings.json` to the latest tag using Python:
   ```bash
   python3 -c "
   import json, pathlib
   p = pathlib.Path('.claude/settings.json')
   d = json.loads(p.read_text())
   d['skills_version'] = '<latest-tag>'
   p.write_text(json.dumps(d, indent=2) + '\n')
   "
   ```

8. Write the upstream version to `.claude/skills_version.txt` so the last-synced state is tracked:
   ```bash
   echo "<latest-tag>" > .claude/skills_version.txt
   ```
   This file is the marker that `finalize-ticket` and `/contribute-skill` use to detect drift.

9. Print a summary:
   ```
   Updated skills from <old-version> → <latest-tag>
   ✓ .claude/commands/update-skills.md
   ✓ .claude/commands/contribute-skill.md
   ... (each file updated)
   ```

## Notes

- Only update files under `.claude/commands/` — never touch `settings.json` hooks or permissions.
- Locally modified skill files are overwritten. If you have local customizations you want to keep, use `/contribute-skill` first to open an upstream PR.
- If the GitHub API is unreachable, print a warning and exit without modifying any files.
