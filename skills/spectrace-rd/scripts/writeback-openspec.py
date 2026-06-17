#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    current_session_meta,
    data_artifact_path,
    format_duration,
    load_workspace_config,
    now_iso,
    openspec_artifact_hashes,
    openspec_bridge_context_path,
    openspec_change_dir,
    openspec_writeback_dir,
    read_json,
    read_text,
    report_artifact_path,
    todo_path,
    workflow_source,
    workspace_root,
    write_managed_json,
    write_managed_text,
)


def list_items(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    return []


def summarize_review(review: dict) -> list[str]:
    findings = list_items(review.get("findings", []))
    if not findings:
        return ["暂无 review finding。"]
    lines: list[str] = []
    for item in findings[:8]:
        if isinstance(item, dict):
            lines.append(f"[{item.get('severity', 'info')}] {item.get('title', '')}：{item.get('detail', '')}")
        else:
            lines.append(f"[info] {str(item)}")
    return lines


def summarize_verify(verify: dict) -> list[str]:
    sections = list_items(verify.get("sections", []))
    if not sections:
        return ["暂无 verify 明细。"]
    lines: list[str] = []
    for section in sections[:8]:
        if not isinstance(section, dict):
            lines.append(str(section))
            continue
        duration = section.get("duration", 0)
        try:
            duration_text = f"{float(duration):.2f}"
        except (TypeError, ValueError):
            duration_text = "0.00"
        lines.append(
            f"{section.get('name', 'unknown')}：{section.get('result', '')}，退出码 {section.get('exit_code', '')}，耗时 {duration_text} 秒"
        )
    return lines


def summarize_acceptance(plan: dict, verify: dict) -> list[dict[str, object]]:
    verify_result = str(verify.get("result", "missing"))
    status = "passed" if verify_result == "通过" else "pending"
    items: list[dict[str, object]] = []
    for criterion in list_items(plan.get("acceptance_criteria", [])):
        if isinstance(criterion, dict):
            task_title = str(criterion.get("task_title") or criterion.get("title") or criterion.get("name") or "")
            checks = [str(item) for item in list_items(criterion.get("checks", []))]
        else:
            task_title = str(criterion)
            checks = []
        if not task_title.strip():
            task_title = "未命名验收项"
        items.append(
            {
                "task_title": task_title,
                "status": status,
                "checks": checks,
            }
        )
    return items


def infer_spec_impacts(plan: dict, bridge_context: dict) -> list[str]:
    impacts = [str(item) for item in list_items(bridge_context.get("spec_reference_files", [])) if str(item).strip()]
    if impacts:
        return impacts
    paths: list[str] = []
    for item in list_items(plan.get("target_codebases", [])):
        if isinstance(item, dict):
            path = str(item.get("path", "")).strip()
        else:
            path = str(item).strip()
        if path:
            paths.append(path)
    return paths


def residual_risks(plan: dict, review: dict, verify: dict) -> list[str]:
    risks = [str(item) for item in list_items(plan.get("risks", [])) if str(item).strip()]
    for finding in list_items(review.get("findings", [])):
        if isinstance(finding, dict):
            if str(finding.get("severity", "")) in ("warning", "blocker"):
                risks.append(str(finding.get("detail", "")))
        elif str(finding).strip():
            risks.append(str(finding))
    if verify.get("result") not in ("通过", "", None):
        risks.append(f"验证结果未通过：{verify.get('result')}")
    return risks[:12]


def task_mapping(plan: dict, verify: dict, todo_text: str) -> list[dict[str, object]]:
    verify_result = str(verify.get("result", "")).strip()
    default_status = "verified" if verify_result == "通过" else "implemented"
    mappings: list[dict[str, object]] = []
    for module in list_items(plan.get("task_modules", [])):
        if not isinstance(module, dict):
            continue
        module_title = str(module.get("title", "")).strip()
        for task in list_items(module.get("tasks", [])):
            if not isinstance(task, dict):
                continue
            title = str(task.get("title", "")).strip()
            if not title:
                continue
            mappings.append(
                {
                    "openspec_task": title,
                    "todo_task": title,
                    "module": module_title,
                    "status": default_status,
                    "evidence": {
                        "impacted_files": list_items(plan.get("impacted_files", [])),
                        "review_result": str(plan.get("review_result", "")),
                        "verify_result": verify_result,
                    },
                }
            )
    if mappings:
        return mappings
    for line in todo_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ["):
            continue
        title = stripped.split("]", 1)[-1].strip()
        if title:
            mappings.append(
                {
                    "openspec_task": title,
                    "todo_task": title,
                    "module": "",
                    "status": default_status,
                    "evidence": {
                        "impacted_files": list_items(plan.get("impacted_files", [])),
                        "verify_result": verify_result,
                    },
                }
            )
    return mappings


def build_markdown(payload: dict) -> str:
    lines = [
        "# SpecTrace Execution Summary",
        "",
        "## Change",
        f"- change: {payload.get('change_name', '')}",
        f"- change_dir: {payload.get('change_dir', '')}",
        f"- session_id: {payload.get('session_id', '')}",
        f"- updated_at: {payload.get('updated_at', '')}",
        "",
        "## Plan",
        f"- requirement_summary: {payload.get('plan', {}).get('requirement_summary', '')}",
        f"- confidence: {payload.get('plan', {}).get('confidence', '')}",
        "",
        "## Repositories",
    ]
    repos = list_items(payload.get("plan", {}).get("target_codebases", []))
    if repos:
        for repo in repos:
            if isinstance(repo, dict):
                lines.append(f"- {repo.get('name', '')}: {repo.get('path', '')}")
            else:
                lines.append(f"- {str(repo)}")
    else:
        lines.append("- 暂无")
    lines.extend([
        "",
        "## Review",
        f"- result: {payload.get('review', {}).get('result', 'missing')}",
    ])
    lines.extend(f"- {item}" for item in payload.get("review_summary", []))
    lines.extend([
        "",
        "## Verify",
        f"- result: {payload.get('verify', {}).get('result', 'missing')}",
    ])
    lines.extend(f"- {item}" for item in payload.get("verify_summary", []))
    lines.extend([
        "",
        "## Task Mapping",
    ])
    mapping = list_items(payload.get("task_mapping", []))
    if mapping:
        for item in mapping[:30]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('todo_task', '')}: {item.get('status', '')}")
    else:
        lines.append("- 暂无")
    lines.extend([
        "",
        "## Links",
        f"- plan.md: {payload.get('reports', {}).get('plan_md', '')}",
        f"- review.md: {payload.get('reports', {}).get('review_md', '')}",
        f"- verify.md: {payload.get('reports', {}).get('verify_md', '')}",
    ])
    lines.append("")
    return "\n".join(lines)


def read_plan_context(config: dict, session_meta: dict) -> dict:
    summary = read_json(data_artifact_path(config, "plan-summary.json", session_meta), {})
    if summary:
        summary["compact"] = True
        return summary
    return read_json(data_artifact_path(config, "plan.json", session_meta), {})


def main() -> None:
    parser = argparse.ArgumentParser(description="把当前会话执行结果回写到 OpenSpec change 目录。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    args = parser.parse_args()

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    if workflow_source(config) != "openspec":
        raise SystemExit("当前 workspace.yml 未启用 OpenSpec 模式，无需执行 writeback-openspec。")

    session_meta = current_session_meta(config)
    plan = read_plan_context(config, session_meta)
    review = read_json(data_artifact_path(config, "review.json", session_meta), {})
    verify = read_json(data_artifact_path(config, "verify.json", session_meta), {})
    status = read_json(data_artifact_path(config, "status.json", session_meta), {})
    todo_text = read_text(todo_path(config))
    plan_bridge_context = plan.get("bridge_context", {})
    if not isinstance(plan_bridge_context, dict) or not plan_bridge_context:
        plan_bridge_context = read_json(openspec_bridge_context_path(config), {})
    if not isinstance(plan_bridge_context, dict):
        plan_bridge_context = {}
    archive_ready = bool(
        review.get("result") == "passed"
        and verify.get("result") == "通过"
        and str(status.get("phase", "")).strip() == "done"
    )
    archive_blockers: list[str] = []
    if review.get("result") != "passed":
        archive_blockers.append("review 未通过")
    if verify.get("result") != "通过":
        archive_blockers.append("verify 未通过")
    if str(status.get("phase", "")).strip() != "done":
        archive_blockers.append(f"当前状态不是 done：{status.get('phase', '')}")

    mapping = task_mapping(plan, verify, todo_text)
    payload = {
        "change_name": str(config.get("openspec", {}).get("change_name", "")),
        "change_dir": str(openspec_change_dir(config)),
        "session_id": session_meta["session_id"],
        "updated_at": now_iso(),
        "status": {
            "phase": status.get("phase", ""),
            "progress": status.get("progress", 0),
            "current_task": status.get("current_task", ""),
            "duration": format_duration(float(status.get("duration_seconds", 0) or 0)),
        },
        "archive_ready": archive_ready,
        "archive_blockers": archive_blockers,
        "plan": {
            "requirement_summary": plan.get("requirement_summary", ""),
            "confidence": plan.get("confidence", ""),
            "target_codebases": plan.get("target_codebases", []),
            "impacted_files": plan.get("impacted_files", []),
            "acceptance_criteria": plan.get("acceptance_criteria", []),
        },
        "bridge_context": plan_bridge_context,
        "review": {
            "result": review.get("result", "missing"),
            "findings": review.get("findings", []),
        },
        "verify": {
            "result": verify.get("result", "missing"),
            "sections": verify.get("sections", []),
        },
        "acceptance_result": summarize_acceptance(plan, verify),
        "task_mapping": mapping,
        "openspec_hashes": openspec_artifact_hashes(config),
        "spec_impacts": infer_spec_impacts(plan, plan_bridge_context),
        "residual_risks": residual_risks(plan, review, verify),
        "manual_decisions": [],
        "review_summary": summarize_review(review),
        "verify_summary": summarize_verify(verify),
        "reports": {
            "plan_md": str(report_artifact_path(config, "plan.md", session_meta)),
            "review_md": str(report_artifact_path(config, "review.md", session_meta)),
            "verify_md": str(report_artifact_path(config, "verify.md", session_meta)),
        },
    }

    output_dir = openspec_writeback_dir(config)
    write_managed_json(config, output_dir / "execution-summary.json", payload)
    write_managed_json(config, output_dir / "task-mapping.json", {"source": "run-workflow.py writeback-openspec", "items": mapping})
    write_managed_text(config, output_dir / "execution-summary.md", build_markdown(payload))
    print(f"writeback_dir={output_dir}")
    print(f"change_dir={openspec_change_dir(config)}")


if __name__ == "__main__":
    main()
