"""Build one real-world case into a folder.

    uv run python -m bench.realworld.build hubspot-215 [--dest DIR]

The same as `python -m bench.realworld build`; see bench/realworld/lib.py for the layout.
"""

from __future__ import annotations

import sys

from bench.realworld.__main__ import main

if __name__ == "__main__":
    sys.exit(main(["build", *sys.argv[1:]]))
