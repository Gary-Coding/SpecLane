#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    current_session_meta,
    data_artifact_path,
    ensure_status,
    file_sha256,
    is_standard_workflow_notification,
    load_workspace_config,
    openspec_change_dir,
    openspec_hash_drift,
    openspec_writeback_dir,
    collect_openspec_cli_context,
    read_json,
    update_st_state,
    workflow_source,
    workspace_root,
    write_managed_json,
    write_managed_text,
)


def detect_spec_conflicts(summary: dict) -> list[dict[str, object]]:
    bridge_context = summary.get("bridge_context", {})
    conflicts: list[dict[str, object]] = []
    for item in bridge_context.get("spec_merge_targets", []):
        target = Path(str(item.get("target", "")))
        baseline = str(item.get("target_sha256", ""))
        current = file_sha256(target)
        target_exists = bool(item.get("target_exists", False))
        if not target_exists and target.exists():
            conflicts.append(
                {
                    "relative_path": str(item.get("relative_path", "")),
                    "reason": "计划阶段目标 spec 不存在，但归档前已出现同路径文件",
                    "target": str(target),
                }
            )
        elif target_exists and baseline and current and baseline != current:
            conflicts.append(
                {
                    "relative_path": str(item.get("relative_path", "")),
                    "reason": "目标 spec 自计划阶段以来已发生变化",
                    "target": str(target),
                }
            )
    return conflicts


def notification_blockers(config: dict, summary: dict) -> list[str]:
    session_meta = current_session_meta(config)
    status = ensure_status(config, session_meta, read_json(data_artifact_path(config, "status.json", session_meta), {}))
    verify = read_json(data_artifact_path(config, "verify.json", session_meta), {})
    notification = read_json(data_artifact_path(config, "notification.json", session_meta), {})
    overall_result = str(verify.get("result") or summary.get("verify", {}).get("result", "")).strip()
    if overall_result != "通过":
        return ["verify 未通过，不能归档"]
    if not is_standard_workflow_notification(config, session_meta, status, overall_result, notification):
        return ["缺少标准 notification.json，或通知不是由 run-workflow.py verify 发送"]
    return []


def build_markdown(payload: dict) -> str:
    blockers = payload.get("blockers", [])
    lines = [
        "# Archive Input",
        "",
        "## Summary",
        f"- change: {payload.get('change_name', '')}",
        f"- archive_ready: {payload.get('archive_ready', False)}",
        "",
        "## Blockers",
    ]
    if blockers:
        lines.extend(f"- {item}" for item in blockers)
    else:
        lines.append("- 暂无")
    lines.extend([
        "",
        "## Spec Impacts",
    ])
    impacts = payload.get("spec_impacts", [])
    if impacts:
        lines.extend(f"- {item}" for item in impacts)
    else:
        lines.append("- 暂无")
    lines.extend([
        "",
        "## Acceptance",
    ])
    acceptance = payload.get("acceptance_result", [])
    if acceptance:
        lines.extend(f"- {item.get('task_title', '')}: {item.get('status', '')}" for item in acceptance)
    else:
        lines.append("- 暂无")
    lines.extend([
        "",
        "## Merge Mode",
        f"- {payload.get('merge_mode', '')}",
        "",
        "## Conflicts",
    ])
    conflicts = payload.get("spec_conflicts", [])
    if conflicts:
        lines.extend(f"- {item.get('relative_path', '')}: {item.get('reason', '')}" for item in conflicts)
    else:
        lines.append("- 暂无")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="根据 OpenSpec 回写结果生成 archive 输入。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    args = parser.parse_args()

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    if workflow_source(config) != "openspec":
        raise SystemExit("当前 workspace.yml 未启用 OpenSpec 模式，无需执行 prepare-archive-openspec。")

    writeback_dir = openspec_writeback_dir(config)
    summary = read_json(writeback_dir / "execution-summary.json", {})
    if not summary:
        raise SystemExit(f"未找到 execution-summary.json：{writeback_dir / 'execution-summary.json'}")

    blockers = list(summary.get("archive_blockers", []))
    if not summary.get("task_mapping"):
        blockers.append("缺少 OpenSpec task -> todo -> evidence 映射")
    acceptance_result = summary.get("acceptance_result", [])
    if any(item.get("status") != "passed" for item in acceptance_result):
        blockers.append("存在未通过的验收项")
    hash_drifts = openspec_hash_drift(config, summary.get("openspec_hashes", {}))
    blockers.extend(hash_drifts)
    blockers.extend(notification_blockers(config, summary))
    spec_conflicts = detect_spec_conflicts(summary)
    if spec_conflicts:
        blockers.append("存在 spec merge 冲突，请先人工处理")
    merge_mode = "safe_merge" if not spec_conflicts else "manual_merge_required"
    openspec_cli = collect_openspec_cli_context(config, include_archive=True)
    status_json = ((openspec_cli.get("status") or {}).get("json") or {})
    artifacts = status_json.get("artifacts", []) if isinstance(status_json, dict) else []
    incomplete_artifacts = []
    for item in artifacts:
        status = str(item.get("status", "")).strip()
        artifact_id = str(item.get("id") or item.get("artifact") or item.get("name") or "").strip()
        if status and status != "done":
            incomplete_artifacts.append({"id": artifact_id, "status": status})
    if incomplete_artifacts:
        blockers.append("OpenSpec status 存在未完成 artifact")

    payload = {
        "change_name": summary.get("change_name", ""),
        "change_dir": str(openspec_change_dir(config)),
        "archive_ready": not blockers and bool(summary.get("archive_ready")),
        "blockers": blockers,
        "review_result": summary.get("review", {}).get("result", ""),
        "verify_result": summary.get("verify", {}).get("result", ""),
        "spec_impacts": summary.get("spec_impacts", []),
        "spec_conflicts": spec_conflicts,
        "openspec_hash_drifts": hash_drifts,
        "merge_mode": merge_mode,
        "acceptance_result": acceptance_result,
        "residual_risks": summary.get("residual_risks", []),
        "reports": summary.get("reports", {}),
        "openspec_cli": openspec_cli,
        "openspec_incomplete_artifacts": incomplete_artifacts,
    }
    write_managed_json(config, writeback_dir / "archive-input.json", payload)
    write_managed_text(config, writeback_dir / "merge-preview.md", build_markdown(payload))
    update_st_state(
        config,
        phase="archive_ready" if payload["archive_ready"] and merge_mode == "safe_merge" else "blocked",
        last_command="/st:archive-check",
        artifacts={
            "archive_input": str(writeback_dir / "archive-input.json"),
            "merge_preview": str(writeback_dir / "merge-preview.md"),
        },
        blocked_reason="; ".join(blockers),
    )
    print(f"archive_ready={str(payload['archive_ready']).lower()}")
    print(f"writeback_dir={writeback_dir}")


if __name__ == "__main__":
    main()
