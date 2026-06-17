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
    phase_after,
    planned_codebases,
    read_json,
    report_artifact_path,
    workspace_root,
    write_managed_json,
    write_managed_text,
)


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def is_git_repo(workspace: Path) -> bool:
    result = run_git(["git", "rev-parse", "--is-inside-work-tree"], workspace)
    return result.returncode == 0 and result.stdout.strip() == "true"


def changed_files(workspace: Path) -> list[str]:
    result = run_git(["git", "status", "--porcelain"], workspace)
    if result.returncode != 0:
        return []

    files: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        normalized = path.strip()
        if normalized.startswith(".spectrace/"):
            continue
        files.append(str((workspace / normalized).resolve()))
    return files


def diff_summary(workspace: Path) -> list[str]:
    result = run_git(["git", "diff", "--stat"], workspace)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    lines: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.rstrip()
        if not stripped or "file changed" in stripped or "files changed" in stripped:
            continue
        if ".spectrace/" in stripped:
            continue
        lines.append(stripped)
    return lines


def compact_lines(lines: list[str], limit: int = 80) -> list[str]:
    if len(lines) <= limit:
        return lines
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    return lines[:head] + [f"... 已省略 {len(lines) - limit} 行，详见 git diff。"] + lines[-tail:]


def read_plan_context(config: dict, session_meta: dict) -> dict:
    summary = read_json(data_artifact_path(config, "plan-summary.json", session_meta), {})
    if summary:
        summary["compact"] = True
        return summary
    return read_json(data_artifact_path(config, "plan.json", session_meta), {})


def find_tests(files: list[str]) -> list[str]:
    return [item for item in files if item.endswith("Test.java") or "/test/" in item]


def build_findings(plan_files: list[str], changed: list[str], repo_mode: str, repo_name: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    planned = set(plan_files)
    changed_set = set(changed)

    if repo_mode == "git" and not changed:
        return [
            {
                "severity": "blocker",
                "scope": repo_name,
                "title": "未检测到代码差异",
                "detail": f"{repo_name}：未检测到本地代码差异，本次审查只能围绕计划范围进行。",
                "blocking": True,
            }
        ]

    if repo_mode == "fallback":
        findings.append(
            {
                "severity": "warning",
                "scope": repo_name,
                "title": "无法读取 Git 差异",
                "detail": f"{repo_name}：当前无法读取 Git 代码差异，本次审查只能基于计划范围做兜底判断。",
                "blocking": False,
            }
        )

    unplanned = sorted(item for item in changed_set if item not in planned and not item.startswith(".spectrace/"))
    if unplanned:
        findings.append(
            {
                "severity": "warning",
                "scope": repo_name,
                "title": "计划外改动",
                "detail": f"{repo_name}：发现计划外改动文件：{', '.join(unplanned)}。请确认是否需要扩展计划范围。",
                "blocking": False,
                "files": unplanned,
            }
        )

    if changed and planned:
        unchanged_planned = sorted(item for item in planned if item not in changed_set)
        if unchanged_planned:
            findings.append(
                {
                    "severity": "info",
                    "scope": repo_name,
                    "title": "计划内文件未改动",
                    "detail": f"{repo_name}：部分计划内文件尚未发生改动：{', '.join(unchanged_planned[:4])}。请确认是尚未实现，还是计划范围过宽。",
                    "blocking": False,
                    "files": unchanged_planned[:8],
                }
            )

    touches_app_code = any(item.endswith((".java", ".kt", ".xml", ".yml")) and "Test" not in item for item in changed)
    if touches_app_code and not find_tests(changed):
        findings.append(
            {
                "severity": "warning",
                "scope": repo_name,
                "title": "缺少测试改动",
                "detail": f"{repo_name}：应用代码已经变更，但没有检测到对应测试改动。请确认是否需要补充测试或人工验证。",
                "blocking": False,
            }
        )

    if not findings:
        findings.append(
            {
                "severity": "info",
                "scope": repo_name,
                "title": "审查通过",
                "detail": f"{repo_name}：当前范围内未发现明显的计划与代码差异不一致问题。",
                "blocking": False,
            }
        )
    return findings


def build_review_markdown(plan: dict, sections: list[dict], findings: list[dict[str, object]]) -> str:
    lines = [
        "# 代码审查",
        "",
        "## 审查范围",
        plan.get("requirement_summary") or "请补充审查范围。",
        "",
        "## 目标仓库",
    ]
    targets = plan.get("target_codebases", [])
    if targets:
        lines.extend(f"- {target.get('name')}：{target.get('path')}" for target in targets)
    else:
        lines.append(f"- {plan.get('resolved_code_path') or '未知'}")
    lines.extend([
        "",
        "## 审查结论",
    ])
    lines.extend(f"- [{item.get('severity')}] {item.get('title')}：{item.get('detail')}" for item in findings)
    lines.extend(["", "## 审查门禁"])
    blocking = [item for item in findings if item.get("blocking")]
    if blocking:
        lines.append("- blocked：存在阻塞项，进入验证前必须处理。")
    else:
        lines.append("- passed：未发现阻塞级问题。")
    lines.extend(["", "## 仓库审查明细"])
    for section in sections:
        lines.extend(
            [
                f"### {section['name']}",
                f"- 审查模式：{'基于 Git 代码差异的审查' if section['repo_mode'] == 'git' else '基于计划范围的兜底审查'}",
                "- 变更文件：",
            ]
        )
        if section["changed"]:
            lines.extend(f"  - `{item}`" for item in section["changed"])
        else:
            lines.append("  - 暂无")
        lines.append("- 差异摘要：")
        if section["summary"]:
            lines.extend(f"  - {item}" for item in section["summary"])
        else:
            lines.append("  - 暂无")
    lines.extend(["", "## 已检查的计划步骤"])
    if plan.get("change_steps"):
        lines.extend(f"- {step['title']}" for step in plan["change_steps"])
    else:
        lines.append("- 暂无")
    lines.extend(["", "## 待复核风险"])
    if plan.get("risks"):
        lines.extend(f"- {item}" for item in plan["risks"])
    else:
        lines.append("- 暂无")
    lines.extend(["", "## 测试覆盖说明"])
    if plan.get("test_plan"):
        lines.extend(f"- {item}" for item in plan["test_plan"])
    else:
        lines.append("- 暂无")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="基于计划和真实代码差异生成 review.md。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    args = parser.parse_args()

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    session_meta = current_session_meta(config)
    plan = read_plan_context(config, session_meta)
    codebases = planned_codebases(config, session_meta)
    target_plans = plan.get("target_codebases", [])
    target_map = {str(item.get("path")): item for item in target_plans}

    sections: list[dict] = []
    findings: list[dict[str, object]] = []
    for codebase in codebases:
        repo_name = codebase.name
        target_plan = target_map.get(str(codebase), {})
        plan_files = target_plan.get("impacted_files", [])
        if is_git_repo(codebase):
            changed = changed_files(codebase)
            summary = compact_lines(diff_summary(codebase))
            repo_mode = "git"
        else:
            changed = plan_files
            summary = []
            repo_mode = "fallback"
        sections.append(
            {
                "name": repo_name,
                "path": str(codebase),
                "changed": changed,
                "summary": summary,
                "repo_mode": repo_mode,
            }
        )
        findings.extend(build_findings(plan_files, changed, repo_mode, repo_name))
    review_result = "blocked" if any(item.get("blocking") for item in findings) else "passed"
    write_managed_json(
        config,
        data_artifact_path(config, "review.json", session_meta),
        {
            "session_id": session_meta["session_id"],
            "source": "run-workflow.py review",
            "schema_version": 1,
            "result": review_result,
            "sections": sections,
            "findings": findings,
            "created_at": now_iso(),
        },
    )
    write_managed_text(
        config,
        report_artifact_path(config, "review.md", session_meta),
        build_review_markdown(plan, sections, findings) + "\n",
    )

    status = ensure_status(config, session_meta, read_json(data_artifact_path(config, "status.json", session_meta), {}))
    phase, awaiting_confirmation, pending_for, next_action = phase_after("review", config["mode"])
    if review_result == "blocked":
        phase = "blocked"
        awaiting_confirmation = False
        pending_for = ""
        next_action = "请先处理 review 阻塞项，再重新执行 review。"
    status.update(
        {
            "phase": phase,
            "current_task": "代码审查已完成。" if review_result == "passed" else "代码审查发现阻塞项。",
            "progress": 75,
            "awaiting_confirmation": awaiting_confirmation,
            "pending_confirmation_for": pending_for,
            "next_action": next_action,
            "completed_tasks": list(dict.fromkeys(status.get("completed_tasks", []) + ["已完成代码审查"])),
            "blocked_tasks": list(dict.fromkeys(status.get("blocked_tasks", []) + [item["title"] for item in findings if item.get("blocking")])),
            "started_at": status.get("started_at") or session_meta.get("started_at", ""),
            "updated_at": now_iso(),
        }
    )
    write_managed_json(config, data_artifact_path(config, "status.json", session_meta), status)
    if review_result == "blocked":
        raise SystemExit("代码审查发现阻塞项，请查看 review.md。")


if __name__ == "__main__":
    main()
