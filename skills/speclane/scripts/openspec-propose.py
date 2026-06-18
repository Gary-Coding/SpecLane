#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    existing_reference_files,
    load_workspace_config,
    openspec_change_dir,
    openspec_change_name,
    openspec_cli_available,
    openspec_writeback_dir,
    read_demand_source,
    run_openspec_cli,
    select_openspec_change,
    summarize_markdown_file,
    update_sl_state,
    validate_openspec_change_name,
    validate_openspec_change_artifacts,
    workflow_source,
    workspace_root,
    write_active_openspec_change,
    write_managed_json,
    write_managed_text,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenSpec-native propose preparation for current workspace.")
    parser.add_argument("change_name", nargs="?", help="OpenSpec change 名称，例如 demand-addition-rate")
    parser.add_argument("--change", dest="change_name_option", help="OpenSpec change 名称，例如 demand-addition-rate")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    parser.add_argument("--demand", help="需求实例名称，用于多需求状态隔离。")
    args = parser.parse_args()
    if getattr(args, "demand", None):
        import os

        os.environ["SPECLANE_DEMAND_NAME"] = args.demand

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    if workflow_source(config) != "openspec":
        raise SystemExit("当前 workspace.yml 未启用 OpenSpec 模式，无法执行 openspec-propose。")

    explicit_change_name = args.change_name_option or args.change_name
    if not explicit_change_name:
        raise SystemExit("缺少 OpenSpec change 名称。请使用 /sl:propose <change-name> 显式指定。")
    explicit_change_name = validate_openspec_change_name(explicit_change_name)
    config = select_openspec_change(config, explicit_change_name)
    change_name = openspec_change_name(config)
    change_dir = openspec_change_dir(config)
    writeback_dir = openspec_writeback_dir(config)
    try:
        demand_source = read_demand_source(config)
    except RuntimeError as error:
        raise SystemExit(str(error))
    demand_file = str(demand_source.get("source", "")).strip()
    demand_text = str(demand_source.get("content", "")).strip()
    demand_keywords = [item for item in [change_name, *change_name.replace("-", " ").split()] if item]
    reference_contexts = [
        summarize_markdown_file(Path(item), keywords=demand_keywords)
        for item in existing_reference_files(config)
    ]

    commands: list[dict] = []
    if openspec_cli_available():
        if not change_dir.exists():
            commands.append(run_openspec_cli(config, ["new", "change", change_name]))
        commands.append(run_openspec_cli(config, ["status", "--change", change_name, "--json"]))
        status_json = commands[-1].get("json") or {}
        artifacts = status_json.get("artifacts", []) if isinstance(status_json, dict) else []
        for artifact in artifacts:
            artifact_id = str(artifact.get("id") or artifact.get("artifact") or artifact.get("name") or "").strip()
            if artifact_id:
                commands.append(run_openspec_cli(config, ["instructions", artifact_id, "--change", change_name, "--json"]))
    else:
        change_dir.mkdir(parents=True, exist_ok=True)
        (change_dir / "specs").mkdir(parents=True, exist_ok=True)
        commands.append(
            {
                "available": False,
                "args": [],
                "returncode": None,
                "stdout": "",
                "stderr": "openspec CLI not found in PATH; created change directory only",
                "json": None,
            }
        )

    active_change_file = write_active_openspec_change(config, change_name)
    payload = {
        "change_name": change_name,
        "change_dir": str(change_dir),
        "active_change_file": str(active_change_file),
        "demand_file": demand_file,
        "demand_source_type": demand_source.get("source_type", ""),
        "demand_fetch_command": demand_source.get("command", []),
        "demand_text_available_at": demand_file,
        "demand_excerpt": demand_text[:12000] + ("\n\n...[已摘要，按需读取 demand_file 全文]..." if len(demand_text) > 12000 else ""),
        "reference_files": reference_contexts,
        "openspec_cli_available": openspec_cli_available(),
        "commands": commands,
        "next_action": "Use demand_excerpt, reference file summaries, and OpenSpec instructions to create or update proposal.md, design.md, tasks.md, and specs/. Read full source files only when necessary.",
        "workflow_phase_after_completion": "proposed",
        "allowed_next_after_completion": ["/sl:bridge"],
        "forbidden_next_after_completion": ["/sl:plan", "/sl:apply"],
        "final_reply_constraint": "代码未修改。下一步只能执行 /sl:bridge。",
    }
    write_managed_json(config, writeback_dir / "propose-input.json", payload)
    write_managed_text(
        config,
        writeback_dir / "propose-input.md",
        "\n".join(
            [
                "# Propose Input",
                "",
                f"- change: {change_name}",
                f"- change_dir: {change_dir}",
                f"- demand_file: {demand_file or ''}",
                f"- demand_source_type: {demand_source.get('source_type', '')}",
                f"- demand_fetch_command: {' '.join(str(item) for item in demand_source.get('command', [])) if demand_source.get('command') else ''}",
                f"- openspec_cli_available: {openspec_cli_available()}",
                "",
                "## Demand",
                "",
                payload["demand_excerpt"] or "未配置或未找到 demand_file。",
                "",
                "## Reference Files",
                "",
                "\n\n".join(
                    [
                        "\n".join(
                            [
                                f"### {item['path']}",
                                "",
                                f"- bytes: {item.get('bytes', 0)}",
                                f"- sha256: {item.get('sha256', '')}",
                                f"- truncated: {item.get('truncated', False)}",
                                "",
                                "#### Headings",
                                "",
                                "\n".join(f"- {heading}" for heading in item.get("headings", [])) or "暂无标题",
                                "",
                                "#### Excerpt",
                                "",
                                item.get("excerpt", "") or "文件为空或无法读取。",
                            ]
                        )
                        for item in reference_contexts
                    ]
                )
                or "未配置或未找到 reference_files。",
                "",
            ]
        ),
    )
    artifact_paths = {
        "proposal": str(change_dir / "proposal.md"),
        "design": str(change_dir / "design.md"),
        "tasks": str(change_dir / "tasks.md"),
        "change_dir": str(change_dir),
        "propose_input": str(writeback_dir / "propose-input.json"),
    }
    validation = validate_openspec_change_artifacts(config)
    if not validation.get("valid"):
        update_sl_state(
            config,
            phase="blocked",
            lasl_command="/sl:propose",
            artifacts=artifact_paths,
            blocked_reason="; ".join(str(item) for item in validation.get("errors", [])),
        )
        print(f"change_name={change_name}")
        print(f"change_dir={change_dir}")
        print(f"active_change_file={active_change_file}")
        print(f"demand_file={demand_file or ''}")
        print(f"demand_source_type={demand_source.get('source_type', '')}")
        print(f"reference_files={len(reference_contexts)}")
        print(f"openspec_cli_available={str(openspec_cli_available()).lower()}")
        print(f"propose_input={writeback_dir / 'propose-input.json'}")
        print("workflow_phase_after_completion=blocked")
        print("propose_artifacts_valid=false")
        for error in validation.get("errors", []):
            print(f"propose_artifact_error={error}")
        print("next_action=AI 必须根据 propose-input 读取需求并补全 proposal.md、design.md、tasks.md 和必要 specs；补全后重新执行 /sl:propose <change-name> 完成校验。")
        print("final_reply_must=/sl:propose 尚未完成。当前不能执行 /sl:bridge。请先补全 OpenSpec change 文档。")
        raise SystemExit(1)

    update_sl_state(
        config,
        phase="proposed",
        lasl_command="/sl:propose",
        artifacts=artifact_paths,
    )
    print(f"change_name={change_name}")
    print(f"change_dir={change_dir}")
    print(f"active_change_file={active_change_file}")
    print(f"demand_file={demand_file or ''}")
    print(f"demand_source_type={demand_source.get('source_type', '')}")
    print(f"reference_files={len(reference_contexts)}")
    print(f"openspec_cli_available={str(openspec_cli_available()).lower()}")
    print(f"propose_input={writeback_dir / 'propose-input.json'}")
    print("workflow_phase_after_completion=proposed")
    print("propose_artifacts_valid=true")
    print("allowed_next_after_completion=/sl:bridge")
    print("forbidden_next_after_completion=/sl:plan,/sl:apply")
    print("final_reply_must=代码未修改。下一步只能执行 /sl:bridge。")


if __name__ == "__main__":
    main()
