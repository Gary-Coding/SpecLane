from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import unique
from .openspec import openspec_reference_files


def existing_reference_files(config: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for item in config.get("reference_files", []):
        path = Path(str(item))
        if path.exists():
            files.append(str(path.resolve()))
    files.extend(openspec_reference_files(config))
    return unique(files)
