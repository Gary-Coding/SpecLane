from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io_utils import read_json
from .session import artifacts_dir, ensure_runtime_dirs
from .time_utils import now_iso, parse_iso_datetime

def workflow_lock_path(config: dict[str, Any]) -> Path:
    return artifacts_dir(config) / "workflow.lock"


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_workflow_lock(config: dict[str, Any], command: str, stale_seconds: int = 1800) -> Path:
    ensure_runtime_dirs(config)
    path = workflow_lock_path(config)
    payload = {
        "pid": os.getpid(),
        "command": command,
        "created_at": now_iso(),
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags)
    except FileExistsError:
        existing = read_json(path, {})
        pid = int(existing.get("pid", 0) or 0) if isinstance(existing, dict) else 0
        created_at = parse_iso_datetime(str(existing.get("created_at", ""))) if isinstance(existing, dict) else None
        age = (datetime.now(timezone.utc) - created_at).total_seconds() if created_at else stale_seconds + 1
        if age > stale_seconds or not process_is_running(pid):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return acquire_workflow_lock(config, command, stale_seconds=stale_seconds)
        raise RuntimeError(
            f"检测到工作流正在执行：pid={pid}, command={existing.get('command', '') if isinstance(existing, dict) else ''}。"
            "请等待当前命令结束后重试。"
        )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def release_workflow_lock(path: Path | None) -> None:
    if not path:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


