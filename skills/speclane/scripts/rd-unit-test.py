#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
    now_iso,
    planned_codebases,
    read_json,
    report_artifact_path,
    write_managed_json,
    write_managed_text,
    workspace_root,
)


MAX_UNIT_TEST_LOG_CHARS = 12000


def compact_command_output(text: str, max_chars: int = MAX_UNIT_TEST_LOG_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    omitted = len(text) - max_chars
    return text[:head].rstrip() + f"\n\n...[已省略 {omitted} 字符]...\n\n" + text[-tail:].lstrip()


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
        markers = (
            "Tests run:",
            "BUILD SUCCESS",
            "BUILD FAILURE",
            "passed",
            "failed",
            "Ran ",
            "ok",
        )
        if any(marker in stripped for marker in markers):
            if stripped not in seen:
                summaries.append(stripped)
                seen.add(stripped)
    return summaries[:10] if summaries else tail(stdout_text)


def build_markdown(sections: list[dict], result: str, duration: float) -> str:
    lines = [
        "# 单元测试结果",
        "",
        "## 总体结果",
        f"- {result}",
        f"- 单元测试阶段耗时：{format_duration(duration)}",
        "",
        "## 仓库单元测试明细",
    ]
    for section in sections:
        lines.extend(
            [
                f"### {section['name']}",
                f"- 路径：{section['path']}",
                f"- 执行结果：{section['result']}",
                f"- 退出码：{section['exit_code']}",
                f"- 耗时（秒）：{section['duration']:.2f}",
                f"- 执行命令：{section['command'] or '未识别'}",
                "- 标准输出摘要：",
            ]
        )
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
    lines.append("")
    return "\n".join(lines)


def merge_result(current: str, new: str) -> str:
    order = {"通过": 0, "未识别命令": 1, "失败": 2, "超时": 3}
    return new if order.get(new, 0) > order.get(current, 0) else current


def main() -> None:
    parser = argparse.ArgumentParser(description="执行单元测试命令并生成 unit-test.md。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    parser.add_argument("--demand", help="需求实例名称，用于多需求状态隔离。")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args()
    if getattr(args, "demand", None):
        import os

        os.environ["SPECLANE_DEMAND_NAME"] = args.demand

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    session_meta = current_session_meta(config)
    plan = read_json(data_artifact_path(config, "plan-summary.json", session_meta), {}) or read_json(data_artifact_path(config, "plan.json", session_meta), {})
    target_plans = plan.get("target_codebases", [])
    target_map = {str(item.get("path")): item for item in target_plans if isinstance(item, dict)}

    started = time.time()
    sections: list[dict] = []
    overall_result = "通过"
    blocked_reason = ""
    try:
        for codebase in planned_codebases(config, session_meta):
            target_plan = target_map.get(str(codebase), {})
            detected = target_plan.get("detected_project") or detect_project(codebase, config)
            test_command = str(detected.get("test_command", "")).strip()
            if not test_command:
                sections.append(
                    {
                        "name": codebase.name,
                        "path": str(codebase),
                        "command": "",
                        "result": "未识别命令",
                        "exit_code": "n/a",
                        "duration": 0.0,
                        "stdout": "",
                        "stderr": "",
                    }
                )
                overall_result = merge_result(overall_result, "未识别命令")
                blocked_reason = "未识别到单元测试命令"
                continue

            repo_started = time.time()
            result = subprocess.run(
                test_command,
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
                    "command": test_command,
                    "result": repo_result,
                    "exit_code": str(result.returncode),
                    "duration": repo_duration,
                    "stdout": compact_command_output(result.stdout),
                    "stderr": compact_command_output(result.stderr),
                }
            )
            if repo_result != "通过":
                overall_result = merge_result(overall_result, "失败")
                blocked_reason = "单元测试失败"
    except subprocess.TimeoutExpired as error:
        duration = time.time() - started
        codebases = planned_codebases(config, session_meta)
        current = codebases[min(len(sections), len(codebases) - 1)] if codebases else None
        sections.append(
            {
                "name": current.name if current else "未知仓库",
                "path": str(current) if current else "",
                "command": "",
                "result": "超时",
                "exit_code": "timeout",
                "duration": duration,
                "stdout": compact_command_output(error.stdout or ""),
                "stderr": compact_command_output(error.stderr or ""),
            }
        )
        overall_result = "超时"
        blocked_reason = "单元测试执行超时"

    duration = time.time() - started
    payload = {
        "session_id": session_meta["session_id"],
        "source": "run-workflow.py unit-test",
        "schema_version": 1,
        "result": overall_result,
        "sections": sections,
        "duration_seconds": duration,
        "created_at": now_iso(),
    }
    write_managed_json(config, data_artifact_path(config, "unit-test.json", session_meta), payload)
    write_managed_text(config, report_artifact_path(config, "unit-test.md", session_meta), build_markdown(sections, overall_result, duration))

    status_path = data_artifact_path(config, "status.json", session_meta)
    status = ensure_status(config, session_meta, read_json(status_path, {}))
    status.update(
        {
            "phase": "unit_test" if overall_result == "通过" else "blocked",
            "current_task": "单元测试已完成。" if overall_result == "通过" else blocked_reason,
            "progress": 58 if overall_result == "通过" else 55,
            "next_action": "继续执行实现自查。" if overall_result == "通过" else "请先修复单元测试问题后重新执行 /sl:apply。",
            "completed_tasks": list(dict.fromkeys(status.get("completed_tasks", []) + ["已执行单元测试"])),
            "blocked_tasks": list(dict.fromkeys(status.get("blocked_tasks", []) + ([blocked_reason] if blocked_reason else []))),
            "updated_at": now_iso(),
        }
    )
    write_managed_json(config, status_path, status)
    if overall_result != "通过":
        raise SystemExit(f"{blocked_reason}，请查看 unit-test.md。")


if __name__ == "__main__":
    main()
