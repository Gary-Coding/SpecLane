#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    constraint_items,
    current_session_meta,
    data_artifact_path,
    detect_project,
    ensure_status,
    existing_reference_files,
    infer_java_modules,
    is_todo_template_placeholder,
    load_workspace_config,
    now_iso,
    parse_task_blocks,
    parse_task_modules,
    phase_after,
    openspec_bridge_context_path,
    report_artifact_path,
    read_json,
    read_text,
    resolve_target_codebases,
    scan_java_files,
    service_hints,
    summarize_detected_projects,
    summarize_todo,
    todo_path,
    todo_progress,
    unique,
    workspace_root,
    write_managed_json,
    write_managed_text,
)


def build_change_steps(summary: str, detected: dict[str, str], impacted_files: list[str], task_breakdown: list[dict[str, object]]) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = [
        {
            "id": "step-1",
            "title": "分析当前实现",
            "details": "结合 todo、参考文件和代码结构确认真实修改范围。",
            "status": "pending",
        },
        {
            "id": "step-2",
            "title": "按计划实施修改",
            "details": "严格围绕计划影响文件和业务规则推进实现，必要时先修订计划。",
            "status": "pending",
        },
        {
            "id": "step-3",
            "title": "自查并整理差异",
            "details": "在进入正式审查前，先确认改动与需求、风险和测试计划一致。",
            "status": "pending",
        },
    ]

    task_text = " ".join(
        [summary]
        + [str(task.get("title", "")) for task in task_breakdown]
        + [str(detail) for task in task_breakdown for detail in task.get("details", [])]
    )
    if any(path.endswith("Test.java") for path in impacted_files) or "测试" in task_text or "校验" in task_text:
        steps.append(
            {
                "id": "step-4",
                "title": "补齐自动化测试",
                "details": "覆盖正常路径、非法输入和回归风险场景。",
                "status": "pending",
            }
        )

    verify_command = detected.get("verify_command") or "请根据项目实际情况补充验证命令。"
    if verify_command == "multiple":
        verify_command = "按各目标仓库分别执行验证命令。"
    steps.append(
        {
            "id": f"step-{len(steps) + 1}",
            "title": "执行验证",
            "details": f"执行验证命令并记录结论：{verify_command}",
            "status": "pending",
        }
    )
    return steps


def build_acceptance_criteria(task_breakdown: list[dict[str, object]], detected: dict[str, str]) -> list[dict[str, object]]:
    criteria: list[dict[str, object]] = []
    for task in task_breakdown:
        title = str(task.get("title", "")).strip()
        details = [str(item) for item in task.get("details", []) if str(item).strip()]
        checks = [f"需求项已实现：{title}"]
        checks.extend(f"子要求已覆盖：{item}" for item in details[:5])
        checks.append("相关边界场景已通过自查或测试覆盖。")
        criteria.append(
            {
                "task_id": str(task.get("id", "")),
                "task_title": title,
                "checks": checks,
            }
        )
    if not criteria:
        criteria.append(
            {
                "task_id": "workflow",
                "task_title": "本轮工作流",
                "checks": ["todo 中的未完成任务已逐项核对。"],
            }
        )
    if detected.get("verify_command"):
        criteria.append(
            {
                "task_id": "verify",
                "task_title": "自动化验证",
                "checks": [f"验证命令可执行并通过：{detected['verify_command']}"],
            }
        )
    return criteria


def discovery_evidence(discovery: dict, limit: int = 16) -> list[dict[str, object]]:
    if discovery.get("source") == "run-workflow.py discovery-summary":
        return list(discovery.get("evidence", []))[:limit]
    evidence: list[dict[str, object]] = []
    for codebase in discovery.get("codebases", []):
        for match in codebase.get("matches", [])[:limit]:
            evidence.append(
                {
                    "codebase": codebase.get("name", ""),
                    "keyword": match.get("keyword", ""),
                    "file": match.get("file", ""),
                    "line": match.get("line", 0),
                    "snippet": match.get("snippet", ""),
                }
            )
            if len(evidence) >= limit:
                return evidence
    return evidence


def confidence_from_discovery(discovery: dict, impacted_files: list[str]) -> str:
    if discovery.get("source") == "run-workflow.py discovery-summary":
        match_count = int(discovery.get("total_match_count", 0) or 0)
    else:
        match_count = sum(len(codebase.get("matches", [])) for codebase in discovery.get("codebases", []))
    if match_count >= 5 and impacted_files:
        return "high"
    if match_count > 0 or impacted_files:
        return "medium"
    return "low"


def build_plan_markdown(plan: dict) -> str:
    detected_project = plan["detected_project"]
    test_command = detected_project["test_command"] if detected_project["test_command"] != "multiple" else "按各仓库分别执行"
    start_command = detected_project["start_command"] if detected_project["start_command"] != "multiple" else "按各仓库实际需要执行"
    verify_command = detected_project["verify_command"] if detected_project["verify_command"] != "multiple" else "按各仓库分别执行"
    build_tool = detected_project["build_tool"] if detected_project["build_tool"] != "multiple" else "多种构建方式"
    lines = [
        "# 变更计划",
        "",
        "## 限制条件",
    ]
    lines.extend(f"- {item}" for item in plan.get("constraints", [])) if plan.get("constraints") else lines.append("- 暂无")
    lines.extend([
        "",
        "## Todo 进度",
        f"- 未完成任务：{plan.get('todo_progress', {}).get('pending_task_count', 0)}",
        f"- 已完成任务：{plan.get('todo_progress', {}).get('completed_task_count', 0)}",
        f"- 总任务数：{plan.get('todo_progress', {}).get('total_task_count', 0)}",
        "",
        "## 需求摘要",
        plan["requirement_summary"],
        "",
        "## 任务拆解",
    ])
    task_modules = plan.get("task_modules", [])
    if task_modules:
        for module in task_modules:
            lines.append(f"- 模块：{module.get('title') or '未命名模块'}")
            for task in module.get("tasks", []):
                lines.append(f"  - 任务：{task.get('title') or '未命名任务'}")
                details = task.get("details", [])
                if details:
                    lines.extend(f"    - {item}" for item in details)
    else:
        lines.append("- 暂无")
    lines.extend([
        "",
        "## 工作区",
        plan["workspace"],
        "",
        "## 目标代码目录",
        f"- 配置目录：{plan.get('configured_code_path') or '未知'}",
        f"- 实际目录：{plan.get('resolved_code_path') or '未知'}",
        f"- 解析说明：{plan.get('service_resolution', {}).get('selection_reason') or '未提供'}",
        "",
        "## 目标仓库",
    ])
    targets = plan.get("target_codebases", [])
    if targets:
        for target in targets:
            lines.extend(
                [
                    f"- {target.get('name') or '未知仓库'}",
                    f"  - 路径：{target.get('path') or '未知'}",
                    f"  - 语言：{target.get('detected_project', {}).get('language') or '未知'}",
                    f"  - 构建工具：{target.get('detected_project', {}).get('build_tool') or '未知'}",
                    f"  - 验证命令：{target.get('detected_project', {}).get('verify_command') or '未识别'}",
                ]
            )
    else:
        lines.append("- 暂无")
    lines.extend([
        "",
        "## 使用的参考文件",
    ])
    lines.extend(f"- {item}" for item in plan["reference_files_used"]) if plan["reference_files_used"] else lines.append("- 暂无")
    lines.extend(["", "## 检测到的项目信息"])
    lines.extend(
        [
            f"- 语言：{detected_project['language'] or '未知'}",
            f"- 构建工具：{build_tool or '未知'}",
            f"- 测试命令：{test_command or '未识别'}",
            f"- 启动命令：{start_command or '未识别'}",
            f"- 验证命令：{verify_command or '未识别'}",
            "",
            "## 影响模块",
        ]
    )
    lines.extend(f"- {item}" for item in plan["impacted_modules"]) if plan["impacted_modules"] else lines.append("- 暂无识别结果")
    lines.extend(["", "## 影响文件"])
    lines.extend(f"- `{item}`" for item in plan["impacted_files"]) if plan["impacted_files"] else lines.append("- 暂无识别结果")
    lines.extend(["", "## 计划置信度", f"- {plan.get('confidence', 'unknown')}"])
    if plan.get("bridge_context"):
        lines.extend(["", "## Bridge Context"])
        bridge_context = plan.get("bridge_context", {})
        lines.extend(f"- {item}" for item in bridge_context.get("business_constraints", [])) if bridge_context.get("business_constraints") else lines.append("- 暂无")
        if bridge_context.get("compatibility_notes"):
            lines.append("- 兼容/发布提示：")
            lines.extend(f"  - {item}" for item in bridge_context.get("compatibility_notes", []))
    lines.extend(["", "## 定位证据"])
    evidence = plan.get("evidence", [])
    if evidence:
        for item in evidence:
            lines.append(
                f"- `{item.get('file')}`:{item.get('line')} 命中 `{item.get('keyword')}`：{item.get('snippet')}"
            )
    else:
        lines.append("- 暂无，实施前需要继续定位代码入口")
    lines.extend(["", "## 验收标准"])
    for criterion in plan.get("acceptance_criteria", []):
        lines.append(f"- {criterion.get('task_title')}")
        for check in criterion.get("checks", []):
            lines.append(f"  - {check}")
    lines.extend(["", "## 实施切片"])
    for item in plan.get("implementation_slices", []):
        lines.append(f"- {item.get('title')}：{item.get('goal')}")
    lines.extend(["", "## 修改步骤"])
    lines.extend(f"- {item['title']}：{item['details']}" for item in plan["change_steps"])
    lines.extend(["", "## 测试计划"])
    lines.extend(f"- {item}" for item in plan["test_plan"]) if plan["test_plan"] else lines.append("- 暂无")
    lines.extend(["", "## 风险"])
    lines.extend(f"- {item}" for item in plan["risks"]) if plan["risks"] else lines.append("- 暂无")
    lines.extend(["", "## 未知项"])
    lines.extend(f"- {item}" for item in plan["unknowns"]) if plan["unknowns"] else lines.append("- 暂无")
    lines.append("")
    return "\n".join(lines)


def build_plan_summary(plan: dict) -> dict[str, object]:
    return {
        "session_id": plan.get("session_id", ""),
        "source": "run-workflow.py plan-summary",
        "schema_version": 1,
        "requirement_summary": plan.get("requirement_summary", ""),
        "target_codebases": [
            {
                "name": item.get("name", ""),
                "path": item.get("path", ""),
                "verify_command": (item.get("detected_project") or {}).get("verify_command", ""),
            }
            for item in plan.get("target_codebases", [])
        ],
        "impacted_files": plan.get("impacted_files", [])[:80],
        "change_steps": plan.get("change_steps", [])[:20],
        "acceptance_criteria": plan.get("acceptance_criteria", [])[:40],
        "test_plan": plan.get("test_plan", [])[:20],
        "risks": plan.get("risks", [])[:10],
        "unknowns": plan.get("unknowns", [])[:10],
    }


def collect_target_plan_data(config: dict, codebases: list[Path]) -> tuple[list[dict], list[str], list[str]]:
    targets: list[dict] = []
    all_impacted_files: list[str] = []
    all_impacted_modules: list[str] = []
    for codebase in codebases:
        detected = detect_project(codebase, config)
        impacted_files: list[str] = []
        impacted_modules: list[str] = []
        if detected["language"] == "java":
            java_files = scan_java_files(codebase)
            impacted_files = java_files[:8]
            impacted_modules = infer_java_modules(impacted_files)
        targets.append(
            {
                "name": codebase.name,
                "path": str(codebase),
                "detected_project": detected,
                "impacted_modules": impacted_modules,
                "impacted_files": impacted_files,
            }
        )
        all_impacted_files.extend(impacted_files)
        all_impacted_modules.extend(impacted_modules)
    return targets, unique(all_impacted_files), unique(all_impacted_modules)


def merge_discovery_files(discovery: dict, fallback_files: list[str]) -> list[str]:
    files: list[str] = []
    if discovery.get("source") == "run-workflow.py discovery-summary":
        for codebase in discovery.get("codebases", []):
            files.extend(str(item) for item in codebase.get("top_files", []) if item)
        files.extend(fallback_files)
        return unique(files)[:24]
    for codebase in discovery.get("codebases", []):
        for match in codebase.get("matches", []):
            file_path = str(match.get("file", "")).strip()
            if file_path:
                files.append(file_path)
    files.extend(fallback_files)
    return unique(files)[:24]


def build_implementation_slices(task_breakdown: list[dict[str, object]], impacted_files: list[str]) -> list[dict[str, object]]:
    slices: list[dict[str, object]] = []
    for index, task in enumerate(task_breakdown, start=1):
        slices.append(
            {
                "id": f"slice-{index}",
                "title": str(task.get("title", "")).strip() or f"任务 {index}",
                "goal": "完成该任务的最小可验证实现，并同步更新相关测试或人工验证项。",
                "candidate_files": impacted_files[:8],
                "status": "pending",
            }
        )
    return slices


def main() -> None:
    parser = argparse.ArgumentParser(description="根据工作区 todo 和仓库上下文生成智能计划。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    parser.add_argument("--demand", help="需求实例名称，用于多需求状态隔离。")
    args = parser.parse_args()
    if getattr(args, "demand", None):
        import os

        os.environ["SPECLANE_DEMAND_NAME"] = args.demand

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    session_meta = current_session_meta(config)
    todo_file = todo_path(config)
    if not todo_file.exists():
        raise SystemExit(f"未找到 todo 文件：{todo_file}")

    todo_text = read_text(todo_file)
    if is_todo_template_placeholder(todo_text):
        raise SystemExit(
            f"已自动创建或检测到示例 todo 模板：{todo_file}。"
            "请先完善真实需求内容后，再重新执行 plan。"
        )
    codebases, resolution = resolve_target_codebases(config, todo_text)
    constraints = constraint_items(todo_text)
    task_breakdown = parse_task_blocks(todo_text)
    task_modules = parse_task_modules(todo_text)
    progress = todo_progress(todo_text)
    if not task_breakdown:
        raise SystemExit("todo.md 中没有未完成任务，请检查是否已经全部标记完成。")
    summary = summarize_todo(todo_text)
    docs = existing_reference_files(config)
    target_codebases, impacted_files, impacted_modules = collect_target_plan_data(config, codebases)
    discovery = read_json(data_artifact_path(config, "discovery-summary.json", session_meta), {}) or read_json(data_artifact_path(config, "discovery.json", session_meta), {})
    bridge_context = read_json(openspec_bridge_context_path(config), {})
    impacted_files = merge_discovery_files(discovery, impacted_files)
    impacted_modules = infer_java_modules(impacted_files) or impacted_modules
    detected = summarize_detected_projects([item["detected_project"] for item in target_codebases])
    primary_codebase = codebases[0]
    test_plan = []
    for target in target_codebases:
        verify_command = target["detected_project"].get("verify_command", "")
        if verify_command:
            test_plan.append(f"{target['name']}：{verify_command}")
    if not test_plan:
        test_plan.append("请根据项目实际情况补充测试命令。")
    test_plan.append("覆盖本轮需求涉及的正常路径、边界场景和回归风险。")

    plan = {
        "session_id": session_meta["session_id"],
        "source": "run-workflow.py plan",
        "schema_version": 1,
        "constraints": constraints,
        "todo_progress": progress,
        "task_modules": task_modules,
        "task_breakdown": task_breakdown,
        "requirement_summary": summary,
        "assumptions": [
            "优先以 todo 和已配置参考文件作为需求边界判断依据。",
            "如果实现过程中发现计划范围不准确，应先更新计划再继续修改代码。",
        ],
        "workspace": str(workspace),
        "configured_code_path": str(config["code_path"]),
        "resolved_code_path": str(primary_codebase),
        "resolved_code_paths": [str(item) for item in codebases],
        "service_hints": resolution.get("service_hints") or service_hints(todo_text),
        "service_resolution": resolution,
        "target_codebases": target_codebases,
        "reference_files_used": docs,
        "detected_project": detected,
        "impacted_modules": impacted_modules,
        "impacted_files": impacted_files,
        "change_steps": build_change_steps(summary, detected, impacted_files, task_breakdown),
        "confidence": confidence_from_discovery(discovery, impacted_files),
        "evidence": discovery_evidence(discovery),
        "bridge_context": bridge_context,
        "acceptance_criteria": build_acceptance_criteria(task_breakdown, detected),
        "implementation_slices": build_implementation_slices(task_breakdown, impacted_files),
        "test_plan": test_plan,
        "risks": [
            "如果参考文件过时，计划可能低估真实影响范围。",
            "如果当前仓库缺少测试覆盖，回归风险需要靠人工复核兜底。",
            "如果 todo 中服务名描述不准确，可能会选错目标仓库，需要在计划阶段先确认。",
            "如果本轮需求涉及多个独立仓库，需要逐仓校验计划、改动和验证结果是否一致。",
        ],
        "unknowns": [
            "需要确认 todo 中未写明但代码行为依赖的隐含业务规则。",
        ],
    }

    write_managed_json(config, data_artifact_path(config, "plan.json", session_meta), plan)
    write_managed_json(config, data_artifact_path(config, "plan-summary.json", session_meta), build_plan_summary(plan))
    write_managed_text(config, report_artifact_path(config, "plan.md", session_meta), build_plan_markdown(plan) + "\n")

    status = ensure_status(config, session_meta, read_json(data_artifact_path(config, "status.json", session_meta), {}))
    phase, awaiting_confirmation, pending_for, next_action = phase_after("plan", config["mode"])
    status.update(
        {
            "phase": phase,
            "current_task": "已生成变更计划。",
            "progress": 25,
            "awaiting_confirmation": awaiting_confirmation,
            "pending_confirmation_for": pending_for,
            "next_action": next_action,
            "completed_tasks": list(dict.fromkeys(status.get("completed_tasks", []) + ["已生成变更计划"])),
            "blocked_tasks": status.get("blocked_tasks", []),
            "started_at": status.get("started_at") or session_meta.get("started_at", ""),
            "updated_at": now_iso(),
        }
    )
    write_managed_json(config, data_artifact_path(config, "status.json", session_meta), status)


if __name__ == "__main__":
    main()
