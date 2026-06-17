#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from common import (
    current_session_meta,
    data_artifact_path,
    ensure_status,
    load_workspace_config,
    now_iso,
    planned_codebases,
    read_json,
    report_artifact_path,
    workspace_root,
    write_managed_json,
    write_managed_text,
)


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def is_git_repo(path: Path) -> bool:
    result = run_git(["git", "rev-parse", "--is-inside-work-tree"], path)
    return result.returncode == 0 and result.stdout.strip() == "true"


def changed_files(path: Path) -> list[str]:
    result = run_git(["git", "status", "--porcelain"], path)
    if result.returncode != 0:
        return []
    files: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        normalized = line[3:].strip()
        if " -> " in normalized:
            normalized = normalized.split(" -> ", 1)[1]
        if normalized.startswith(".speclane/"):
            continue
        files.append(str((path / normalized).resolve()))
    return files


def build_self_check(plan: dict, sections: list[dict]) -> dict:
    findings: list[dict] = []
    plan_files = set(str(item) for item in plan.get("impacted_files", []))
    changed = {item for section in sections for item in section.get("changed_files", [])}
    has_git_repo = any(section.get("repo_mode") == "git" for section in sections)
    changed_app = [item for item in changed if item.endswith((".java", ".kt", ".xml", ".yml", ".yaml", ".ts", ".tsx", ".js"))]
    changed_tests = [item for item in changed if "/test/" in item or item.endswith(("Test.java", ".test.ts", ".spec.ts", ".test.js"))]

    if not changed and has_git_repo:
        findings.append(
            {
                "severity": "blocker",
                "title": "未检测到代码差异",
                "detail": "实现阶段结束后没有发现本地代码变更，无法进入有效审查。",
                "blocking": True,
            }
        )
    elif not changed:
        findings.append(
            {
                "severity": "warning",
                "title": "无法读取代码差异",
                "detail": "目标目录不是 Git 仓库，自查只能依赖后续人工 review 与验证命令。",
                "blocking": False,
            }
        )
    unplanned = sorted(item for item in changed if plan_files and item not in plan_files)
    if unplanned:
        findings.append(
            {
                "severity": "warning",
                "title": "存在计划外改动",
                "detail": "请确认这些文件是否应补入计划：" + "，".join(unplanned[:8]),
                "blocking": False,
            }
        )
    if changed_app and not changed_tests:
        findings.append(
            {
                "severity": "warning",
                "title": "应用代码改动缺少测试改动",
                "detail": "如果本轮改动无法自动化测试，需要在 verify.md 中补充人工验证项。",
                "blocking": False,
            }
        )
    if not findings:
        findings.append(
            {
                "severity": "info",
                "title": "自查通过",
                "detail": "未发现会阻断 review 的基础问题。",
                "blocking": False,
            }
        )
    return {
        "result": "blocked" if any(item.get("blocking") for item in findings) else "passed",
        "findings": findings,
    }


def build_markdown(self_check: dict, sections: list[dict]) -> str:
    lines = [
        "# 实现自查",
        "",
        "## 总体结果",
        f"- {self_check.get('result')}",
        "",
        "## 自查发现",
    ]
    for finding in self_check.get("findings", []):
        lines.append(
            f"- [{finding.get('severity')}] {finding.get('title')}：{finding.get('detail')}"
        )
    lines.extend(["", "## 变更文件"])
    for section in sections:
        lines.append(f"### {section.get('name')}")
        changed = section.get("changed_files", [])
        if changed:
            lines.extend(f"- `{item}`" for item in changed)
        else:
            lines.append("- 暂无")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="实现完成后进行基础自查并生成 self-check 产物。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    args = parser.parse_args()

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    session_meta = current_session_meta(config)
    plan = read_json(data_artifact_path(config, "plan-summary.json", session_meta), {}) or read_json(data_artifact_path(config, "plan.json", session_meta), {})

    sections: list[dict] = []
    for codebase in planned_codebases(config, session_meta):
        sections.append(
            {
                "name": codebase.name,
                "path": str(codebase),
                "repo_mode": "git" if is_git_repo(codebase) else "fallback",
                "changed_files": changed_files(codebase) if is_git_repo(codebase) else [],
            }
        )
    self_check = build_self_check(plan, sections)
    payload = {
        "session_id": session_meta["session_id"],
        "source": "run-workflow.py self-check",
        "schema_version": 1,
        "result": self_check["result"],
        "sections": sections,
        "findings": self_check["findings"],
        "created_at": now_iso(),
    }
    write_managed_json(config, data_artifact_path(config, "self-check.json", session_meta), payload)
    write_managed_text(config, report_artifact_path(config, "self-check.md", session_meta), build_markdown(self_check, sections))

    status = ensure_status(config, session_meta, read_json(data_artifact_path(config, "status.json", session_meta), {}))
    status.update(
        {
            "phase": "self_check" if self_check["result"] == "passed" else "blocked",
            "current_task": "实现自查已完成。",
            "progress": 65 if self_check["result"] == "passed" else 62,
            "next_action": "继续执行代码审查。" if self_check["result"] == "passed" else "请先处理自查阻塞项。",
            "completed_tasks": list(dict.fromkeys(status.get("completed_tasks", []) + ["已完成实现自查"])),
            "blocked_tasks": list(dict.fromkeys(status.get("blocked_tasks", []) + [item["title"] for item in self_check["findings"] if item.get("blocking")])),
            "updated_at": now_iso(),
        }
    )
    write_managed_json(config, data_artifact_path(config, "status.json", session_meta), status)
    if self_check["result"] == "blocked":
        raise SystemExit("实现自查发现阻塞项，请查看 self-check.md。")


if __name__ == "__main__":
    main()
