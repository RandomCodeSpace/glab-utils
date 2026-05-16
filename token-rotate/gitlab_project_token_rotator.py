#!/usr/bin/env python3
"""CLI wrapper for the importable token_rotate rotator package."""

from __future__ import annotations

import sys

from token_rotate.rotator import main


if __name__ == "__main__":
    sys.exit(main())
