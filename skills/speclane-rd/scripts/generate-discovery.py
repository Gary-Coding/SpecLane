#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from common import (
    current_session_meta,
    data_artifact_path,
    detect_project,
    ensure_status,
    extract_todo_keywords,
    load_workspace_config,
    now_iso,
    parse_task_blocks,
    read_json,
    read_text,
    relative_to,
    report_artifact_path,
    resolve_target_codebases,
    todo_path,
    workspace_root,
    write_managed_json,
    write_managed_text,
)


def run_rg(keyword: str, codebase: Path, max_count: int = 12) -> list[dict[str, object]]:
    if not shutil.which("rg"):
        return []
    result = subprocess.run(
        [
            "rg",
            "--line-number",
            "--column",
            "--no-heading",
            "--fixed-strings",
            "--glob",
            "!target/**",
            "--glob",
            "!build/**",
            "--glob",
            "!node_modules/**",
            "--glob",
            "!.git/**",
            keyword,
            str(codebase),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        return []
    matches: list[dict[str, object]] = []
    for line in result.stdout.splitlines()[:max_count]:
        parts = line.split(":", 3)
        if len(parts) < 4:
            continue
        file_path, line_no, column_no, snippet = parts
        matches.append(
            {
                "file": str(Path(file_path).resolve()),
                "relative_file": relative_to(file_path, codebase),
                "line": int(line_no) if line_no.isdigit() else 0,
                "column": int(column_no) if column_no.isdigit() else 0,
                "snippet": snippet.strip()[:240],
            }
        )
    return matches


def build_discovery_markdown(discovery: dict) -> str:
    lines = [
        "# 上下文定位",
        "",
        "## 任务",
    ]
    tasks = discovery.get("tasks", [])
    if tasks:
        lines.extend(f"- {task.get('title')}" for task in tasks)
    else:
        lines.append("- 暂无")
    lines.extend(["", "## 关键词"])
    keywords = discovery.get("keywords", [])
    lines.extend(f"- `{item}`" for item in keywords) if keywords else lines.append("- 暂无")
    lines.extend(["", "## 代码命中"])
    for codebase in discovery.get("codebases", []):
        lines.append(f"### {codebase.get('name')}")
        lines.append(f"- 路径：{codebase.get('path')}")
        lines.append(f"- 构建工具：{codebase.get('detected_project', {}).get('build_tool') or '未知'}")
        matches = codebase.get("matches", [])
        if not matches:
            lines.append("- 未命中关键词")
            continue
        for item in matches[:20]:
            lines.append(
                f"- `{item.get('relative_file')}`:{item.get('line')} "
                f"命中 `{item.get('keyword')}`：{item.get('snippet')}"
            )
    lines.extend(["", "## 计划提示"])
    for hint in discovery.get("planning_hints", []):
        lines.append(f"- {hint}")
    lines.append("")
    return "\n".join(lines)


def build_discovery_summary(discovery: dict) -> dict[str, object]:
    codebase_summaries: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    total_matches = 0
    for codebase in discovery.get("codebases", []):
        matches = list(codebase.get("matches", []))
        total_matches += len(matches)
        codebase_summaries.append(
            {
                "name": codebase.get("name", ""),
                "path": codebase.get("path", ""),
                "detected_project": codebase.get("detected_project", {}),
                "match_count": len(matches),
                "top_files": list(dict.fromkeys(str(item.get("file", "")) for item in matches if item.get("file")))[:12],
            }
        )
        for item in matches[:6]:
            evidence.append(
                {
                    "codebase": codebase.get("name", ""),
                    "keyword": item.get("keyword", ""),
                    "file": item.get("file", ""),
                    "line": item.get("line", 0),
                    "snippet": item.get("snippet", ""),
                }
            )
            if len(evidence) >= 20:
                break
    return {
        "session_id": discovery.get("session_id", ""),
        "source": "run-workflow.py discovery-summary",
        "schema_version": 1,
        "task_count": len(discovery.get("tasks", [])),
        "keywords": discovery.get("keywords", [])[:30],
        "service_resolution": discovery.get("service_resolution", {}),
        "codebases": codebase_summaries,
        "total_match_count": total_matches,
        "evidence": evidence,
        "planning_hints": discovery.get("planning_hints", [])[:10],
        "created_at": discovery.get("created_at", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="根据 todo 关键词定位代码证据，生成 discovery 产物。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    args = parser.parse_args()

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    session_meta = current_session_meta(config)
    todo_text = read_text(todo_path(config))
    codebases, resolution = resolve_target_codebases(config, todo_text)
    keywords = extract_todo_keywords(todo_text)
    if not keywords:
        keywords = [task["title"] for task in parse_task_blocks(todo_text)[:8]]

    codebase_sections: list[dict] = []
    all_matches: list[dict] = []
    for codebase in codebases:
        matches: list[dict] = []
        for keyword in keywords:
            for match in run_rg(keyword, codebase):
                match["keyword"] = keyword
                matches.append(match)
        seen: set[tuple[str, int, str]] = set()
        deduped: list[dict] = []
        for match in matches:
            key = (str(match.get("file", "")), int(match.get("line", 0)), str(match.get("keyword", "")))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(match)
        all_matches.extend(deduped)
        codebase_sections.append(
            {
                "name": codebase.name,
                "path": str(codebase),
                "detected_project": detect_project(codebase, config),
                "matches": deduped[:80],
            }
        )

    planning_hints = []
    if all_matches:
        planning_hints.append("优先把命中关键词的文件作为影响范围候选，并结合调用链继续定位。")
    else:
        planning_hints.append("关键词未命中代码，计划阶段需要基于项目结构继续定位真实入口。")
    if len(codebases) > 1:
        planning_hints.append("本轮涉及多个仓库，后续 review 和 verify 必须逐仓执行。")

    discovery = {
        "session_id": session_meta["session_id"],
        "tasks": parse_task_blocks(todo_text),
        "keywords": keywords,
        "service_resolution": resolution,
        "codebases": codebase_sections,
        "planning_hints": planning_hints,
        "created_at": now_iso(),
    }
    write_managed_json(config, data_artifact_path(config, "discovery.json", session_meta), discovery)
    write_managed_json(config, data_artifact_path(config, "discovery-summary.json", session_meta), build_discovery_summary(discovery))
    write_managed_text(config, report_artifact_path(config, "discovery.md", session_meta), build_discovery_markdown(discovery))

    status = ensure_status(config, session_meta, read_json(data_artifact_path(config, "status.json", session_meta), {}))
    status.update(
        {
            "phase": "discover",
            "current_task": "已完成上下文定位。",
            "progress": 15,
            "next_action": "基于 discovery.json 生成可执行计划。",
            "completed_tasks": list(dict.fromkeys(status.get("completed_tasks", []) + ["已完成上下文定位"])),
            "updated_at": now_iso(),
        }
    )
    write_managed_json(config, data_artifact_path(config, "status.json", session_meta), status)


if __name__ == "__main__":
    main()
