#!/usr/bin/env python3
"""Entry point of the russian-voiceover-51 skill.

Usage: python <skill folder>/scripts/pipeline.py <command> ...   (start with: doctor)
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

if sys.version_info < (3, 10):
    sys.exit(f"Python 3.10 or newer is needed, this is {sys.version.split()[0]}.")

try:
    from rupipe.cli import main
except ImportError as e:
    sys.exit(f"Missing Python package: {e.name}.\n"
             f"Install the packages into this Python:\n"
             f"  \"{sys.executable}\" -m pip install -r \"{HERE / 'requirements.txt'}\"")

if __name__ == "__main__":
    sys.exit(main())
