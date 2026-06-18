from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import write_json, write_text
from .session import artifacts_dir, output_dir, qa_dir, workflow_source


def _openspec_writeback_roots(config: dict[str, Any]) -> list[Path]:
    if workflow_source(config) != "openspec":
        return []
    openspec = config.get("openspec", {})
    writeback = Path(str(openspec.get("writeback_dir", ""))).resolve()
    change_dir = Path(str(openspec.get("change_dir", ""))).resolve()
    roots = [writeback] if str(writeback) else []
    if str(change_dir):
        roots.append((change_dir.parent / "archive").resolve())
    return roots


def assert_managed_artifact(path: Path, config: dict[str, Any]) -> Path:
    resolved = path.resolve()
    allowed_roots = [
        artifacts_dir(config).resolve(),
        output_dir(config).resolve(),
        qa_dir(config).resolve(),
        *_openspec_writeback_roots(config),
    ]
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise ValueError(f"拒绝写入非工作流托管产物：{resolved}")
    return resolved


def write_managed_json(config: dict[str, Any], path: Path, payload: Any) -> None:
    write_json(assert_managed_artifact(path, config), payload)


def write_managed_text(config: dict[str, Any], path: Path, content: str) -> None:
    write_text(assert_managed_artifact(path, config), content)
