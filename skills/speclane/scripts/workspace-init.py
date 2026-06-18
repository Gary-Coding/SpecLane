#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    ensure_runtime_dirs,
    ensure_workflow_inputs,
    load_workspace_config,
    read_text,
    todo_path,
    code_root,
    output_dir,
    workflow_source,
    workspace_root,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 speclane 工作区基础目录。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    parser.add_argument("--demand", help="需求实例名称，用于多需求状态隔离。")
    args = parser.parse_args()
    if getattr(args, "demand", None):
        import os

        os.environ["SPECLANE_DEMAND_NAME"] = args.demand

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    if not workspace.exists():
        raise SystemExit(f"工作区不存在：{workspace}")
    if not code_root(config).exists():
        raise SystemExit(f"代码目录不存在：{code_root(config)}")

    ensure_runtime_dirs(config)

    try:
        input_result = ensure_workflow_inputs(config)
    except (FileNotFoundError, ValueError) as error:
        raise SystemExit(str(error))
    todo_file = todo_path(config)
    print(f"workflow_source={workflow_source(config)}")
    print(f"todo_created={'true' if input_result.get('todo_created') else 'false'}")
    print(f"bridge_generated={'true' if input_result.get('bridge_generated') else 'false'}")
    if input_result.get("bridge_source"):
        print(f"bridge_source={input_result.get('bridge_source')}")
    todo_text = read_text(todo_file)
    print(f"todo_needs_edit={'true' if input_result.get('todo_needs_edit') else 'false'}")

    print(f"workspace={workspace}")
    print(f"todo={todo_file}")
    print(f"code_path={code_root(config)}")
    print(f"data_root={workspace / '.speclane'}")
    print(f"output_dir={output_dir(config)}")
    print(f"skill_config={config.get('__skill_config_path', '')}")
    if todo_text.strip():
        print("todo_status=ready")

    if bool(config.get("__skill_config_created", False)):
        print("skill_config_created=true")
        raise SystemExit(
            "已自动生成 Skill 配置文件："
            f"{config.get('__skill_config_path', '')}。"
            "当前工作流已暂停。"
            "请先完善配置后再继续。"
        )
    print("skill_config_created=false")


if __name__ == "__main__":
    main()
