#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import current_session_meta, data_artifact_path, ensure_status, load_workspace_config, now_iso, read_json, unique, workspace_root, write_managed_json


def main() -> None:
    parser = argparse.ArgumentParser(description="更新 speclane 的 status.json。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    parser.add_argument("--demand", help="需求实例名称，用于多需求状态隔离。")
    parser.add_argument("--phase", required=True)
    parser.add_argument("--progress", type=int, required=True)
    parser.add_argument("--current-task", default="")
    parser.add_argument("--awaiting-confirmation", action="store_true")
    parser.add_argument("--pending-confirmation-for", default="")
    parser.add_argument("--next-action", default="")
    parser.add_argument("--completed-task", action="append", default=[])
    parser.add_argument("--blocked-task", action="append", default=[])
    args = parser.parse_args()
    if getattr(args, "demand", None):
        import os

        os.environ["SPECLANE_DEMAND_NAME"] = args.demand

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    session_meta = current_session_meta(config)
    status_path = data_artifact_path(config, "status.json", session_meta)
    status = ensure_status(config, session_meta, read_json(status_path, {}))
    status["phase"] = args.phase
    status["progress"] = args.progress
    status["current_task"] = args.current_task
    status["awaiting_confirmation"] = args.awaiting_confirmation
    status["pending_confirmation_for"] = args.pending_confirmation_for
    status["next_action"] = args.next_action
    status["completed_tasks"] = unique(status.get("completed_tasks", []) + args.completed_task)
    status["blocked_tasks"] = unique(status.get("blocked_tasks", []) + args.blocked_task)
    status["started_at"] = status.get("started_at") or session_meta.get("started_at", "")
    status["updated_at"] = now_iso()

    write_managed_json(config, status_path, status)


if __name__ == "__main__":
    main()
