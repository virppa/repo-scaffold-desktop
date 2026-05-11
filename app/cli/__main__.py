"""Allow `python -m app.cli` to invoke main()."""

import sys

from app.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
