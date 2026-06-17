#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import time
from pathlib import Path

from common import (
    current_session_meta,
    data_artifact_path,
    detect_project,
    ensure_status,
    format_duration,
    load_workspace_config,
    is_standard_workflow_notification,
    feishu_config,
    notify_workflow_result,
    now_iso,
    phase_after,
    planned_codebases,
    read_json,
    report_artifact_path,
    workflow_duration_seconds,
    write_managed_json,
    write_managed_text,
    workspace_root,
)


MAX_VERIFY_LOG_CHARS = 12000


def compact_command_output(text: str, max_chars: int = MAX_VERIFY_LOG_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    omitted = len(text) - max_chars
    return text[:head].rstrip() + f"\n\n...[已省略 {omitted} 字符]...\n\n" + text[-tail:].lstrip()


def compact_command_records(value):
    if isinstance(value, dict):
        return {
            key: compact_command_output(item) if key in ("stdout", "stderr", "output", "logs") and isinstance(item, str) else compact_command_records(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [compact_command_records(item) for item in value]
    return value

VERIFY_BLOCKERS = {"未识别到验证命令", "验证失败", "验证执行超时"}


def has_sent_standard_notification(
    config: dict,
    session_meta: dict,
    status: dict,
) -> bool:
    verify_path = data_artifact_path(config, "verify.json", session_meta)
    notification_path = data_artifact_path(config, "notification.json", session_meta)
    verify = read_json(verify_path, {})
    notification = read_json(notification_path, {})
    if not isinstance(verify, dict) or not isinstance(notification, dict):
        return False
    overall_result = str(verify.get("result", "")).strip()
    if not overall_result:
        return False
    return is_standard_workflow_notification(config, session_meta, status, overall_result, notification)


def tail(text: str, lines: int = 20) -> list[str]:
    items = [line.rstrip() for line in text.splitlines() if line.strip()]
    return items[-lines:]


def summarize_stdout(stdout_text: str) -> list[str]:
    summaries: list[str] = []
    seen: set[str] = set()
    for line in stdout_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.search(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)", stripped)
        if match:
            total, failures, errors, skipped = match.groups()
            summary = f"测试统计：共 {total} 条，用例失败 {failures}，错误 {errors}，跳过 {skipped}。"
            if summary not in seen:
                summaries.append(summary)
                seen.add(summary)
        if "BUILD SUCCESS" in stripped:
            summary = "构建结果：成功。"
            if summary not in seen:
                summaries.append(summary)
                seen.add(summary)
        if "BUILD FAILURE" in stripped:
            summary = "构建结果：失败。"
            if summary not in seen:
                summaries.append(summary)
                seen.add(summary)
        if "default message [" in stripped:
            messages = re.findall(r"default message \[([^\]]+)\]", stripped)
            if messages:
                summary = f"参数校验提示：{messages[-1]}"
                if summary not in seen:
                    summaries.append(summary)
                    seen.add(summary)
    return summaries[:10] if summaries else tail(stdout_text)


def write_report(
    config: dict,
    output: Path,
    sections: list[dict],
    checks: list[str],
    result: str,
    verify_duration: float,
    workflow_duration: float,
) -> None:
    lines = [
        "# 验证结果",
        "",
        "## 总体结果",
        f"- {result}",
        f"- 验证阶段耗时：{format_duration(verify_duration)}",
        f"- 整个工作流耗时：{format_duration(workflow_duration)}",
        "",
        "## 计划检查项",
    ]
    lines.extend(f"- {item}" for item in checks) if checks else lines.append("- 暂无")
    lines.extend(["", "## 仓库验证明细"])
    for section in sections:
        lines.extend(
            [
                f"### {section['name']}",
                f"- 路径：{section['path']}",
                f"- 执行结果：{section['result']}",
                f"- 退出码：{section['exit_code']}",
                f"- 耗时（秒）：{section['duration']:.2f}",
                "- 执行命令：",
            ]
        )
        if section["commands"]:
            for item in section["commands"]:
                if isinstance(item, dict):
                    lines.append(f"  - [{item.get('kind')}] {item.get('command')}")
                else:
                    lines.append(f"  - {item}")
        else:
            lines.append("  - 暂无，请补充可执行的验证命令。")
        lines.append("- 标准输出摘要：")
        stdout_summary = summarize_stdout(section["stdout"])
        if section["stdout"].strip():
            lines.extend(f"  - {item}" for item in stdout_summary)
        else:
            lines.append("  - 暂无")
        lines.append("- 标准错误摘要：")
        stderr_summary = tail(section["stderr"])
        if section["stderr"].strip():
            lines.extend(f"  - {item}" for item in stderr_summary)
        else:
            lines.append("  - 暂无")
    lines.extend(["", "## 人工补充验证"])
    if result == "通过":
        lines.append("- 如果本次改动涉及服务启动或接口行为，建议补一次人工 smoke test。")
    elif result == "未识别命令":
        lines.append("- 当前仓库未能自动识别验证命令，请人工补充后再执行。")
    elif result == "超时":
        lines.append("- 验证执行超时，请检查命令是否卡住或是否需要更长超时时间。")
    else:
        lines.append("- 请根据失败输出修复问题后重新执行验证。")
    lines.append("")
    write_managed_text(config, output, "\n".join(lines))


def merge_result(current: str, new: str) -> str:
    order = {"通过": 0, "未识别命令": 1, "失败": 2, "超时": 3}
    return new if order.get(new, 0) > order.get(current, 0) else current


def finalize_status(
    config: dict,
    session_meta: dict,
    plan: dict,
    overall_result: str,
    blocked_reason: str,
) -> None:
    status_path = data_artifact_path(config, "status.json", session_meta)
    status = ensure_status(config, session_meta, read_json(status_path, {}))
    phase, awaiting_confirmation, pending_for, _ = phase_after("verify", config["mode"])
    existing_blockers = [item for item in status.get("blocked_tasks", []) if item not in VERIFY_BLOCKERS]
    finished_at = now_iso()
    duration_seconds = workflow_duration_seconds(session_meta, status, finished_at)

    if overall_result == "通过":
        status.update(
            {
                "phase": "done",
                "current_task": "验证通过。",
                "progress": 100,
                "awaiting_confirmation": False,
                "pending_confirmation_for": "",
                "next_action": "工作流已完成。",
                "completed_tasks": list(dict.fromkeys(status.get("completed_tasks", []) + ["已执行验证"])),
                "blocked_tasks": existing_blockers,
            }
        )
    else:
        status.update(
            {
                "phase": "blocked",
                "current_task": (
                    "未识别到验证命令。"
                    if overall_result == "未识别命令"
                    else ("验证执行超时。" if overall_result == "超时" else "验证失败。")
                ),
                "progress": 95,
                "awaiting_confirmation": awaiting_confirmation if overall_result != "超时" else False,
                "pending_confirmation_for": pending_for if overall_result not in ("通过", "超时") else "",
                "next_action": (
                    "请人工补充验证命令后重新执行验证。"
                    if overall_result == "未识别命令"
                    else ("请检查命令是否卡住，或延长超时时间后重试。" if overall_result == "超时" else "请修复验证失败项后重新执行。")
                ),
                "completed_tasks": list(dict.fromkeys(status.get("completed_tasks", []) + ["已执行验证"])),
                "blocked_tasks": list(dict.fromkeys(existing_blockers + [blocked_reason or "验证失败"])),
            }
        )

    status.update(
        {
            "started_at": status.get("started_at") or session_meta.get("started_at", ""),
            "finished_at": finished_at,
            "duration_seconds": duration_seconds,
            "updated_at": finished_at,
        }
    )

    notification_result = notify_workflow_result(config, session_meta, plan, status, overall_result)
    status["notification_status"] = str(notification_result.get("status", "skipped"))
    status["notification_message"] = str(notification_result.get("message", ""))
    if (
        overall_result == "通过"
        and feishu_config(config).get("enabled")
        and not is_standard_workflow_notification(config, session_meta, status, overall_result, notification_result)
    ):
        status.update(
            {
                "phase": "blocked",
                "current_task": "验证通过但飞书通知发送失败。",
                "progress": 95,
                "awaiting_confirmation": False,
                "pending_confirmation_for": "",
                "next_action": "请修复飞书通知配置或网络后重新执行 /st:verify。",
                "blocked_tasks": list(dict.fromkeys(status.get("blocked_tasks", []) + ["飞书通知发送失败"])),
                "updated_at": now_iso(),
            }
        )
    write_managed_json(config, status_path, status)


def main() -> None:
    parser = argparse.ArgumentParser(description="执行验证命令并生成 verify.md。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--force", action="store_true", help="即使当前会话已完成，也强制重新执行验证并覆盖结果。")
    args = parser.parse_args()

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    session_meta = current_session_meta(config)
    plan = read_json(data_artifact_path(config, "plan-summary.json", session_meta), {}) or read_json(data_artifact_path(config, "plan.json", session_meta), {})
    status_path = data_artifact_path(config, "status.json", session_meta)
    existing_status = ensure_status(config, session_meta, read_json(status_path, {}))
    if (
        not args.force
        and str(existing_status.get("phase", "")).strip() == "done"
        and str(existing_status.get("notification_status", "")).strip() == "sent"
        and has_sent_standard_notification(config, session_meta, existing_status)
    ):
        print("当前会话已完成且通知已发送，跳过重复验证。需要重跑请显式传入 --force。")
        return

    codebases = planned_codebases(config, session_meta)
    target_plans = plan.get("target_codebases", [])
    target_map = {str(item.get("path")): item for item in target_plans}

    started = time.time()
    sections: list[dict] = []
    overall_result = "通过"
    blocked_reason = ""
    try:
        for codebase in codebases:
            target_plan = target_map.get(str(codebase), {})
            detected = target_plan.get("detected_project") or detect_project(codebase, config)
            commands = [
                {"kind": kind, "command": item}
                for kind, item in [
                    ("test", detected.get("test_command", "")),
                    ("start", detected.get("start_command", "")),
                    ("verify", detected.get("verify_command", "")),
                ]
                if item
            ]
            verify_command = detected.get("verify_command", "")
            if not verify_command:
                sections.append(
                    {
                        "name": codebase.name,
                        "path": str(codebase),
                        "commands": commands,
                        "result": "未识别命令",
                        "exit_code": "n/a",
                        "duration": 0.0,
                        "stdout": "",
                        "stderr": "",
                    }
                )
                overall_result = merge_result(overall_result, "未识别命令")
                blocked_reason = "未识别到验证命令"
                continue

            repo_started = time.time()
            result = subprocess.run(
                verify_command,
                cwd=codebase,
                shell=True,
                capture_output=True,
                text=True,
                timeout=args.timeout_seconds,
                check=False,
            )
            repo_duration = time.time() - repo_started
            repo_result = "通过" if result.returncode == 0 else "失败"
            sections.append(
                {
                    "name": codebase.name,
                    "path": str(codebase),
                    "commands": commands,
                    "result": repo_result,
                    "exit_code": str(result.returncode),
                    "duration": repo_duration,
                    "stdout": compact_command_output(result.stdout),
                    "stderr": compact_command_output(result.stderr),
                }
            )
            if repo_result != "通过":
                overall_result = merge_result(overall_result, "失败")
                blocked_reason = "验证失败"
        duration = time.time() - started
        status_for_duration = ensure_status(config, session_meta, read_json(status_path, {}))
        workflow_duration = workflow_duration_seconds(session_meta, status_for_duration, now_iso())
        write_report(
            config,
            report_artifact_path(config, "verify.md", session_meta),
            sections,
            plan.get("test_plan", []),
            overall_result,
            duration,
            workflow_duration,
        )
        write_managed_json(
            config,
            data_artifact_path(config, "verify.json", session_meta),
            {
                "session_id": session_meta["session_id"],
                "source": "run-workflow.py verify",
                "schema_version": 1,
                "result": overall_result,
                "sections": sections,
                "duration_seconds": duration,
                "workflow_duration_seconds": workflow_duration,
                "created_at": now_iso(),
            },
        )
        finalize_status(config, session_meta, plan, overall_result, blocked_reason)
    except subprocess.TimeoutExpired as error:
        duration = time.time() - started
        sections.append(
            {
                "name": codebases[min(len(sections), len(codebases) - 1)].name if codebases else "未知仓库",
                "path": str(codebases[min(len(sections), len(codebases) - 1)]) if codebases else "",
                "commands": [],
                "result": "超时",
                "exit_code": "timeout",
                "duration": duration,
                "stdout": compact_command_output(error.stdout or ""),
                "stderr": compact_command_output(error.stderr or ""),
            }
        )
        status_for_duration = ensure_status(config, session_meta, read_json(status_path, {}))
        workflow_duration = workflow_duration_seconds(session_meta, status_for_duration, now_iso())
        write_report(
            config,
            report_artifact_path(config, "verify.md", session_meta),
            sections,
            plan.get("test_plan", []),
            "超时",
            duration,
            workflow_duration,
        )
        write_managed_json(
            config,
            data_artifact_path(config, "verify.json", session_meta),
            {
                "session_id": session_meta["session_id"],
                "source": "run-workflow.py verify",
                "schema_version": 1,
                "result": "超时",
                "sections": sections,
                "duration_seconds": duration,
                "workflow_duration_seconds": workflow_duration,
                "created_at": now_iso(),
            },
        )
        finalize_status(config, session_meta, plan, "超时", "验证执行超时")


if __name__ == "__main__":
    main()
