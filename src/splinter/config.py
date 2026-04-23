from __future__ import annotations

import tomllib
from pathlib import Path


def load_project_config(cwd: Path) -> dict:
    """Load [tool.splinter] from the nearest pyproject.toml above cwd, or return {}."""
    pyproject = _find_pyproject(cwd.resolve())
    if pyproject is None:
        return {}
    try:
        with open(pyproject, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return {}
    return data.get("tool", {}).get("splinter", {})


def _find_pyproject(start: Path) -> Path | None:
    current = start
    while True:
        candidate = current / "pyproject.toml"
        if candidate.exists():
            return candidate
        if current.parent == current:
            return None
        current = current.parent
