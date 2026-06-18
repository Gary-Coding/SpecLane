#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lib.artifact_guard import assert_managed_artifact, write_managed_json, write_managed_text
from lib.references import existing_reference_files
from lib.todo import (
    constraint_items,
    ensure_workflow_inputs,
    extract_todo_keywords,
    is_constraint_section,
    is_task_section,
    is_todo_template_placeholder,
    normalize_todo_text_item,
    parse_task_blocks,
    parse_task_modules,
    parse_todo_document,
    parse_todo_sections,
    service_hints,
    summarize_todo,
    todo_items,
    todo_progress,
    todo_template,
)
from lib.workspace import (
    active_demand_path,
    apply_demand_defaults,
    configured_demand_name,
    default_skill_config,
    default_skill_config_text,
    demand_registry_dir,
    demand_runtime_dir,
    ensure_user_skill_config,
    expand_workspace_variables,
    load_skill_config,
    load_workspace_config,
    load_workspace_demand_config,
    normalize_verify_commands,
    read_active_demand,
    resolve_requested_demand,
    resolve_workspace_path,
    skill_config_example_path,
    skill_config_path,
    skill_root,
    user_skill_config_dir,
    validate_demand_name,
    workspace_config_path,
    workspace_demands,
    workspace_root,
    write_active_demand,
)
from lib.lock import (
    acquire_workflow_lock,
    process_is_running,
    release_workflow_lock,
    workflow_lock_path,
)
from lib.lark import (
    demand_path,
    is_lark_doc_url,
    is_url,
    lark_cli_available,
    lark_cli_install_message,
    read_demand_source,
    read_lark_doc_demand,
)
from lib.io_utils import (
    compact_text_excerpt,
    file_sha256,
    markdown_headings,
    read_json,
    read_text,
    relative_to,
    summarize_markdown_file,
    unique,
    write_json,
    write_text,
)
from lib.time_utils import format_duration, now_iso, parse_iso_datetime
from lib.yaml_utils import parse_scalar, parse_simple_yaml

from lib.project_detect import (
    code_root,
    detect_project,
    find_candidate_codebases,
    infer_java_modules,
    looks_like_project_root,
    resolve_target_codebase,
    resolve_target_codebases,
    scan_java_files,
    summarize_detected_projects,
    todo_path,
)
from lib.state import (
    RUN_COMMAND_TO_SL_COMMAND,
    SL_COMMAND_TO_RUN_COMMAND,
    SL_PHASE_ALLOWED_NEXT,
    TODO_PHASE_ALLOWED_NEXT,
    default_status,
    ensure_status,
    phase_after,
    read_sl_state,
    recover_sl_state_from_artifacts,
    recover_todo_state_from_artifacts,
    recover_workflow_state_from_artifacts,
    update_sl_state,
    validate_sl_state,
    validate_standard_session,
    write_sl_state,
)
from lib.session import (
    active_openspec_change_path,
    active_session_for_plan,
    artifact_path,
    artifacts_dir,
    create_session,
    current_session_file,
    current_session_is_stale,
    current_session_meta,
    current_session_status,
    data_artifact_path,
    ensure_plan_can_run,
    ensure_runtime_dirs,
    output_dir,
    planned_codebase,
    planned_codebases,
    qa_dir,
    report_artifact_path,
    session_data_dir,
    session_report_dir,
    sessions_dir,
    sl_state_path,
    todo_state_path,
    workflow_source,
    workflow_state_path,
    workspace_relative_path,
)
from lib.openspec import (
    build_openspec_bridge_context,
    collect_openspec_cli_context,
    infer_openspec_service_hints,
    is_openspec_placeholder_text,
    openspec_archive_root,
    openspec_artifact_hashes,
    openspec_bridge_context_path,
    openspec_change_dir,
    openspec_change_name,
    openspec_cli_available,
    openspec_hash_drift,
    openspec_reference_files,
    openspec_root,
    openspec_source_texts,
    openspec_tasks_hash,
    openspec_tasks_path,
    openspec_writeback_dir,
    run_openspec_cli,
    select_openspec_change,
    transform_openspec_tasks_to_todo,
    validate_openspec_change_artifacts,
    validate_openspec_change_name,
    write_active_openspec_change,
)
from lib.notify import (
    build_feishu_notification_payload,
    build_workflow_notification,
    feishu_config,
    feishu_sign,
    is_standard_workflow_notification,
    notify_workflow_result,
    notification_has_sent_route,
    pushplus_config,
    send_feishu_notification,
    send_pushplus_notification,
    workflow_duration_seconds,
    workflow_notification_fingerprint,
)


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
