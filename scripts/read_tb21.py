#!/usr/bin/env python3
"""Launch the artifact reader without installing evaluation dependencies."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skillexpand.reader.app import main

if __name__ == "__main__":
    main()
