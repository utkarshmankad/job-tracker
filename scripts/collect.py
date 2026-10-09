#!/usr/bin/env python3
"""Local application-history collector for Job Tracker (runs on your own computer).

Usage: python scripts/collect.py --help   (see docs/collector-operations.md)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
