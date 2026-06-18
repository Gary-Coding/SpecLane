#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    current_session_meta,
    data_artifact_path,
    existing_reference_files,
    load_workspace_config,
    now_iso,
    openspec_change_dir,
    qa_dir,
    read_json,
    read_text,
    report_artifact_path,
    summarize_markdown_file,
    todo_path,
    workflow_source,
    write_managed_json,
    write_managed_text,
)


def qa_artifact(config: dict, name: str) -> Path:
    return qa_dir(config) / name


def current_rd_context(config: dict) -> dict:
    try:
        session_meta = current_session_meta(config)
    except FileNotFoundError:
        session_meta = {}
    context = {
        "session": session_meta,
        "plan": {},
        "review": {},
        "verify": {},
        "reports": {},
    }
    if session_meta:
        context["plan"] = read_json(data_artifact_path(config, "plan-summary.json", session_meta), {}) or read_json(data_artifact_path(config, "plan.json", session_meta), {})
        context["review"] = read_json(data_artifact_path(config, "review.json", session_meta), {})
        context["verify"] = read_json(data_artifact_path(config, "verify.json", session_meta), {})
        for name in ("plan.md", "review.md", "verify.md"):
            path = report_artifact_path(config, name, session_meta)
            if path.exists():
                context["reports"][name] = str(path)
    return context


def build_payload(config: dict) -> dict:
    todo = todo_path(config)
    rd = current_rd_context(config)
    references = [
        summarize_markdown_file(Path(path), max_excerpt_chars=1600)
        for path in existing_reference_files(config)
        if Path(path).exists() and Path(path).is_file()
    ]
    openspec = {}
    if workflow_source(config) == "openspec":
        change_dir = openspec_change_dir(config)
        openspec = {
            "change_dir": str(change_dir),
            "proposal": summarize_markdown_file(change_dir / "proposal.md", max_excerpt_chars=1600),
            "design": summarize_markdown_file(change_dir / "design.md", max_excerpt_chars=1600),
            "tasks": summarize_markdown_file(change_dir / "tasks.md", max_excerpt_chars=2000),
        }
    return {
        "source": "speclane qa-plan",
        "generated_at": now_iso(),
        "demand_name": str(config.get("__demand_name", "")),
        "workflow_source": workflow_source(config),
        "todo": summarize_markdown_file(todo, max_excerpt_chars=2400) if todo.exists() else {},
        "openspec": openspec,
        "rd": rd,
        "references": references,
    }


def build_markdown(payload: dict) -> str:
    rd = payload.get("rd", {}) if isinstance(payload.get("rd"), dict) else {}
    plan = rd.get("plan", {}) if isinstance(rd.get("plan"), dict) else {}
    verify = rd.get("verify", {}) if isinstance(rd.get("verify"), dict) else {}
    target_codebases = plan.get("target_codebases", []) if isinstance(plan, dict) else []
    changed_files = plan.get("changed_files", []) or plan.get("files", []) if isinstance(plan, dict) else []
    verify_result = str(verify.get("result", "")).strip() if isinstance(verify, dict) else ""
    lines = [
        "# QA 测试计划",
        "",
        f"- 需求：{payload.get('demand_name', '')}",
        f"- 生成时间：{payload.get('generated_at', '')}",
        f"- 工作流来源：{payload.get('workflow_source', '')}",
        f"- RD 会话：{(rd.get('session') or {}).get('session_id', '') if isinstance(rd, dict) else ''}",
        f"- RD 验证结果：{verify_result or '未验证'}",
        "",
        "## 测试范围",
    ]
    if target_codebases:
        for item in target_codebases:
            lines.append(f"- 代码库：{item.get('path', item) if isinstance(item, dict) else item}")
    if changed_files:
        lines.append("")
        lines.append("## 重点变更文件")
        for item in changed_files[:40]:
            lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## 建议测试类型",
            "- 功能测试：覆盖 todo.md 和 OpenSpec tasks.md 中的交付项。",
            "- 回归测试：覆盖变更服务的核心历史流程。",
            "- 异常测试：覆盖参数缺失、非法值、边界条件和权限校验。",
            "- 集成测试：覆盖上下游接口、数据库读写、消息/任务等外部依赖。",
            "",
            "## 测试用例清单",
            "- [ ] 正向主流程验证。",
            "- [ ] 关键业务规则验证。",
            "- [ ] 边界值与异常输入验证。",
            "- [ ] 兼容性/回归验证。",
            "- [ ] 验证 RD 输出文档中的已知风险。",
            "",
            "## 准入条件",
            "- RD verify 已通过，或已明确记录未通过原因。",
            "- 测试环境数据和依赖服务已准备。",
            "- OpenSpec/todo 与实际实现没有已知偏差。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 QA 测试计划。")
    parser.add_argument("--workspace")
    parser.add_argument("--demand")
    args = parser.parse_args()
    if args.demand:
        import os

        os.environ["SPECLANE_DEMAND_NAME"] = args.demand
    config = load_workspace_config(Path(args.workspace).expanduser() if args.workspace else None)
    payload = build_payload(config)
    write_managed_json(config, qa_artifact(config, "test-plan.json"), payload)
    write_managed_text(config, qa_artifact(config, "test-plan.md"), build_markdown(payload))
    print(f"qa_plan={qa_artifact(config, 'test-plan.md')}")
    print("next_action=测试人员审核 test-plan.md，补充真实测试用例后执行 /sl:qa:report。")


if __name__ == "__main__":
    main()
