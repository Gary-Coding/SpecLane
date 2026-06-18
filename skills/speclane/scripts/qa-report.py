#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    current_session_meta,
    data_artifact_path,
    load_workspace_config,
    now_iso,
    qa_dir,
    read_json,
    read_text,
    workflow_source,
    write_managed_json,
    write_managed_text,
)


def qa_artifact(config: dict, name: str) -> Path:
    return qa_dir(config) / name


def build_payload(config: dict) -> dict:
    try:
        session_meta = current_session_meta(config)
    except FileNotFoundError:
        session_meta = {}
    verify = read_json(data_artifact_path(config, "verify.json", session_meta), {}) if session_meta else {}
    review = read_json(data_artifact_path(config, "review.json", session_meta), {}) if session_meta else {}
    plan_text = read_text(qa_artifact(config, "test-plan.md"))
    return {
        "source": "speclane qa-report",
        "generated_at": now_iso(),
        "demand_name": str(config.get("__demand_name", "")),
        "workflow_source": workflow_source(config),
        "rd_session": session_meta,
        "rd_verify_result": str(verify.get("result", "")).strip() if isinstance(verify, dict) else "",
        "rd_review_result": str(review.get("result", "")).strip() if isinstance(review, dict) else "",
        "test_plan_exists": bool(plan_text),
        "conclusion": "待测试执行后填写",
    }


def build_markdown(payload: dict) -> str:
    lines = [
        "# QA 测试报告",
        "",
        f"- 需求：{payload.get('demand_name', '')}",
        f"- 生成时间：{payload.get('generated_at', '')}",
        f"- 工作流来源：{payload.get('workflow_source', '')}",
        f"- RD 会话：{(payload.get('rd_session') or {}).get('session_id', '') if isinstance(payload.get('rd_session'), dict) else ''}",
        f"- RD Review：{payload.get('rd_review_result', '') or '未记录'}",
        f"- RD Verify：{payload.get('rd_verify_result', '') or '未记录'}",
        "",
        "## 测试执行结果",
        "- 结论：待测试执行后填写",
        "- 通过用例：0",
        "- 失败用例：0",
        "- 阻塞用例：0",
        "",
        "## 缺陷记录",
        "- 暂无。若发现缺陷，请记录现象、复现步骤、期望结果、实际结果和影响范围。",
        "",
        "## 回归建议",
        "- 对本次变更影响的核心业务链路进行回归。",
        "- 若存在缺陷修复，应创建新的 RD 修复需求或补充当前需求后重新进入交付链路。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 QA 测试报告草稿。")
    parser.add_argument("--workspace")
    parser.add_argument("--demand")
    args = parser.parse_args()
    if args.demand:
        import os

        os.environ["SPECLANE_DEMAND_NAME"] = args.demand
    config = load_workspace_config(Path(args.workspace).expanduser() if args.workspace else None)
    payload = build_payload(config)
    write_managed_json(config, qa_artifact(config, "test-report.json"), payload)
    write_managed_text(config, qa_artifact(config, "test-report.md"), build_markdown(payload))
    print(f"qa_report={qa_artifact(config, 'test-report.md')}")
    print("next_action=测试人员补充真实执行结果；若失败，形成缺陷并回到 RD 修复链路。")


if __name__ == "__main__":
    main()
