Run a security audit on the current changes.

### 1. Static analysis
Run bandit on the app directory:
```bash
bandit -r app/ -c pyproject.toml -f txt
```
Show the full output.

### 2. Diff review
Get the current diff against main:
```bash
git diff main
```

Review the diff for OWASP Top 10 patterns relevant to a Python desktop/CLI tool:
- **A01 Broken Access Control** — unauthorized file access, path traversal (`../` in user-supplied paths)
- **A02 Cryptographic Failures** — hardcoded secrets, tokens, or keys in source
- **A03 Injection** — `subprocess` with `shell=True`, f-strings interpolated into shell commands
- **A04 Insecure Design** — missing input validation, trusting user-supplied paths without sanitization
- **A05 Security Misconfiguration** — debug flags left on, permissive file permissions (0o777)
- **A08 Software/Data Integrity** — unvalidated Jinja2 template inputs, unsafe deserialization

### 3. Verdict
For each finding, state: **severity** (HIGH / MEDIUM / LOW), **file:line**, **description**, **fix**.

End with an explicit verdict:
- PASS — no issues found, safe to proceed to `/finalize-ticket`
- WARNINGS — low-severity issues noted, can proceed with awareness
- FAIL — issues must be fixed before creating a PR
