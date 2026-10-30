"""Sanitize a diagnostic screenshot to a narrow page title banner."""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    with Image.open(args.source) as image:
        image = image.convert("RGB")
        width, height = image.size
        if width != 1440 or height != 900:
            raise SystemExit("unexpected_viewport")
        # Keep only the page title strip. Diagnostic tables, identifiers and
        # fingerprint values below this strip are excluded by construction.
        image.crop((0, 0, min(width, 900), min(height, 64))).save(args.destination, "PNG", optimize=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
