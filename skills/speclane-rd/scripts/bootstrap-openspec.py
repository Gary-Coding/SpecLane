#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    ensure_workflow_inputs,
    load_workspace_config,
    openspec_tasks_hash,
    todo_path,
    update_sl_state,
    validate_openspec_change_artifacts,
    workflow_source,
    workspace_root,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="根据 OpenSpec change/tasks 生成桥接 todo 文件。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    parser.add_argument("--demand", help="需求实例名称，用于多需求状态隔离。")
    parser.add_argument("--explicit-sl-bridge", action="store_true", help="确认本次调用来自用户显式 /sl:bridge 命令。")
    args = parser.parse_args()
    if getattr(args, "demand", None):
        import os

        os.environ["SPECLANE_DEMAND_NAME"] = args.demand

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    if workflow_source(config) != "openspec":
        raise SystemExit("当前 workspace.yml 未启用 OpenSpec 模式，无需执行 bootstrap-openspec。")
    if not args.explicit_sl_bridge:
        raise SystemExit(
            "拒绝执行桥接：bootstrap-openspec 只能由用户显式 /sl:bridge 触发。"
            "如果当前命令是 /sl:propose、/sl:init、/sl:plan 或 /sl:apply，必须停止，不能生成 todo.md。"
        )
    validation = validate_openspec_change_artifacts(config)
    if not validation.get("valid"):
        for error in validation.get("errors", []):
            print(f"propose_artifact_error={error}")
        raise SystemExit("OpenSpec change 文档未完成，禁止 /sl:bridge。请先补全 proposal.md、design.md、tasks.md 和必要 specs 后重新执行 /sl:propose <change-name>。")
    result = ensure_workflow_inputs(config, allow_bridge_write=True)
    update_sl_state(
        config,
        phase="bridged",
        lasl_command="/sl:bridge",
        artifacts={
            "todo": str(todo_path(config)),
            "bridge_source": str(result.get("bridge_source", "")),
            "tasks_sha256": openspec_tasks_hash(config),
        },
    )
    print(f"workflow_source={result.get('workflow_source', '')}")
    print(f"todo={todo_path(config)}")
    print(f"bridge_generated={'true' if result.get('bridge_generated') else 'false'}")
    print(f"bridge_source={result.get('bridge_source', '')}")
    print(f"tasks_sha256={openspec_tasks_hash(config)}")


if __name__ == "__main__":
    main()
