"""The release tag, pyproject, and `spelunk.__version__` must all agree.

CI's publish workflow refuses a tag that disagrees with pyproject.toml. Nothing was checking
`__version__`, so it silently rotted (0.0.1 while the package shipped 0.1.0) and the MCP server
advertised that stale value to clients. This closes the loop.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import spelunk

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_dunder_version_matches_pyproject() -> None:
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert spelunk.__version__ == declared, (
        f"spelunk.__version__ ({spelunk.__version__}) != pyproject version ({declared}); "
        "bump both together."
    )
