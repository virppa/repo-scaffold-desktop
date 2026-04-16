"""Wrapper that sets PYTHONUTF8=1 before invoking semgrep.

Required on Windows where the default codepage (cp1252) cannot encode all
characters in Semgrep's --config auto rule downloads. CI sets this via the
workflow env block; pre-commit must set it here instead.
"""

import os
import shutil
import subprocess
import sys

semgrep_bin = shutil.which("semgrep")
if semgrep_bin is None:
    print("semgrep not found in PATH", file=sys.stderr)
    sys.exit(1)

env = os.environ.copy()
env["PYTHONUTF8"] = "1"

result = subprocess.run([semgrep_bin, *sys.argv[1:]], env=env)  # nosec B603
sys.exit(result.returncode)
