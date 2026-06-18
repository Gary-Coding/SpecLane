#!/usr/bin/env python3
from __future__ import annotations

import re
from typing import Any

from lib.artifact_guard import write_managed_json, write_managed_text
from lib.io_utils import (
    file_sha256,
    read_json,
    read_text,
    relative_to,
    summarize_markdown_file,
    unique,
    write_json,
)
from lib.lark import is_lark_doc_url, is_url, read_demand_source
from lib.lock import acquire_workflow_lock, release_workflow_lock, workflow_lock_path
from lib.notify import (
    build_feishu_notification_payload,
    feishu_config,
    feishu_sign,
    is_standard_workflow_notification,
    notify_workflow_result,
    workflow_duration_seconds,
    workflow_notification_fingerprint,
)
from lib.openspec import (
    collect_openspec_cli_context,
    openspec_archive_root,
    openspec_artifact_hashes,
    openspec_bridge_context_path,
    openspec_change_dir,
    openspec_change_name,
    openspec_cli_available,
    openspec_hash_drift,
    openspec_tasks_hash,
    openspec_writeback_dir,
    run_openspec_cli,
    select_openspec_change,
    validate_openspec_change_artifacts,
    validate_openspec_change_name,
    write_active_openspec_change,
)
from lib.project_detect import (
    code_root,
    detect_project,
    infer_java_modules,
    resolve_target_codebases,
    scan_java_files,
    summarize_detected_projects,
    todo_path,
)
from lib.references import existing_reference_files
from lib.session import (
    active_session_for_plan,
    create_session,
    current_session_file,
    current_session_is_stale,
    current_session_meta,
    data_artifact_path,
    ensure_plan_can_run,
    ensure_runtime_dirs,
    output_dir,
    planned_codebase,
    planned_codebases,
    qa_dir,
    report_artifact_path,
    workflow_source,
)
from lib.state import (
    SL_COMMAND_TO_RUN_COMMAND,
    ensure_status,
    phase_after,
    read_sl_state,
    recover_sl_state_from_artifacts,
    recover_workflow_state_from_artifacts,
    update_sl_state,
    validate_sl_state,
    validate_standard_session,
)
from lib.time_utils import format_duration, now_iso
from lib.todo import (
    constraint_items,
    ensure_workflow_inputs,
    extract_todo_keywords,
    is_todo_template_placeholder,
    parse_task_blocks,
    parse_task_modules,
    parse_todo_document,
    service_hints,
    summarize_todo,
    todo_progress,
)
from lib.workspace import (
    active_demand_path,
    configured_demand_name,
    demand_registry_dir,
    demand_runtime_dir,
    expand_workspace_variables,
    load_workspace_config,
    normalize_verify_commands,
    resolve_requested_demand,
    validate_demand_name,
    workspace_config_path,
    workspace_root,
    write_active_demand,
)
from lib.yaml_utils import parse_simple_yaml


def require_sl_state(config: dict[str, Any], run_command: str) -> None:
    result = validate_sl_state(config, run_command)
    if result.get("valid"):
        return
    errors = result.get("errors", [])
    message = "\n".join(str(item) for item in errors) if errors else "当前工作流状态不允许执行该命令。"
    raise SystemExit(message)


def parse_sl_command(text: str) -> dict[str, Any]:
    stripped = str(text).strip()
    match = re.match(r"^(/sl:[a-z][a-z-]*(?::[a-z][a-z-]*)?)(?:\s+([A-Za-z0-9][A-Za-z0-9-]*))?(?:\s|$)", stripped)
    if not match:
        raise ValueError("未识别到 /sl:* 命令。")
    sl_command = match.group(1)
    argument = match.group(2) or ""
    demand_match = re.search(r"(?:^|\s)--demand(?:=|\s+)([A-Za-z0-9][A-Za-z0-9._-]*)", stripped)
    demand_name = validate_demand_name(demand_match.group(1)) if demand_match else ""
    if sl_command not in SL_COMMAND_TO_RUN_COMMAND:
        raise ValueError(f"不支持的 /sl:* 命令：{sl_command}")
    return {
        "sl_command": sl_command,
        "run_command": SL_COMMAND_TO_RUN_COMMAND[sl_command],
        "argument": argument,
        "demand_name": demand_name,
        "raw_text": stripped,
    }
