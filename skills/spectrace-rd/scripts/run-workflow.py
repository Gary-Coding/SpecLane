#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from common import (
    create_session,
    current_session_is_stale,
    current_session_meta,
    data_artifact_path,
    acquire_workflow_lock,
    ensure_plan_can_run,
    ensure_status,
    load_workspace_config,
    now_iso,
    parse_st_command,
    planned_codebases,
    planned_codebase,
    read_json,
    read_st_state,
    release_workflow_lock,
    require_st_state,
    report_artifact_path,
    recover_st_state_from_artifacts,
    todo_path,
    update_st_state,
    validate_standard_session,
    validate_st_state,
    workflow_source,
    write_managed_json,
    workspace_root,
)


SCRIPT_DIR = Path(__file__).resolve().parent


ST_ROUTE_REPLY_CONSTRAINTS: dict[str, dict[str, str]] = {
    "/st:propose": {
        "phase": "proposed",
        "allowed_next": "/st:bridge",
        "forbidden_next": "/st:plan,/st:apply",
        "final_reply_must": "代码未修改。下一步只能执行 /st:bridge。",
    },
    "/st:bridge": {
        "phase": "bridged",
        "allowed_next": "人工审核 todo.md 后 /st:apply",
        "forbidden_next": "自动执行 /st:plan,自动执行 /st:apply,代码实现",
        "final_reply_must": "桥接 todo 已生成。请审核 todo.md，审核通过后发送 /st:apply。",
    },
    "/st:plan": {
        "phase": "planned",
        "allowed_next": "/st:apply",
        "forbidden_next": "代码实现,review,verify",
        "final_reply_must": "计划已生成。下一步执行 /st:apply。",
    },
}


def run_python(script_name: str, extra_args: list[str]) -> None:
    script_path = SCRIPT_DIR / script_name
    result = subprocess.run(
        [sys.executable, str(script_path), *extra_args],
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def load_status(workspace: Path | None) -> tuple[dict, Path]:
    config = load_workspace_config(workspace)
    session_meta = current_session_meta(config)
    status_path = data_artifact_path(config, "status.json", session_meta)
    status = ensure_status(config, session_meta, read_json(status_path, {}))
    return status, status_path


def update_status_for_implement(workspace: Path | None, current_task: str, next_action: str, phase: str, progress: int, completed_task: str | None = None) -> None:
    config = load_workspace_config(workspace)
    session_meta = current_session_meta(config)
    status_path = data_artifact_path(config, "status.json", session_meta)
    status = ensure_status(config, session_meta, read_json(status_path, {}))
    completed_tasks = status.get("completed_tasks", [])
    if completed_task and completed_task not in completed_tasks:
        completed_tasks = completed_tasks + [completed_task]
    status.update(
        {
            "phase": phase,
            "current_task": current_task,
            "progress": progress,
            "awaiting_confirmation": phase.startswith("wait_confirm_"),
            "pending_confirmation_for": "review" if phase == "wait_confirm_implement" else "",
            "next_action": next_action,
            "completed_tasks": completed_tasks,
            "blocked_tasks": status.get("blocked_tasks", []),
            "started_at": status.get("started_at") or session_meta.get("started_at", ""),
            "updated_at": now_iso(),
        }
    )
    write_managed_json(config, status_path, status)


def command_status(workspace: Path | None) -> None:
    config = load_workspace_config(workspace)
    recover_st_state_from_artifacts(config)
    try:
        status, _ = load_status(workspace)
    except FileNotFoundError:
        print("尚未创建当前会话，请先执行 plan。")
        status = {}
    if not status:
        print("尚未生成 status.json，请先执行 plan。")
    else:
        for key in (
            "session_id",
            "mode",
            "phase",
            "current_task",
            "progress",
            "awaiting_confirmation",
            "pending_confirmation_for",
            "next_action",
            "started_at",
            "finished_at",
            "duration_seconds",
            "notification_status",
            "notification_message",
            ):
            print(f"{key}={status.get(key, '')}")
    state = read_st_state(config)
    if state:
        print(f"st_phase={state.get('phase', '')}")
        print(f"st_allowed_next={','.join(str(item) for item in state.get('allowed_next', []))}")
    if workflow_source(config) == "todo":
        standard = validate_standard_session(config, require_notification=False)
        print(f"standard_session={str(bool(standard.get('valid'))).lower()}")
        for error in standard.get("errors", []):
            print(f"standard_error={error}")


def command_assert_standard_session(workspace: Path | None, require_notification: bool = False) -> None:
    config = load_workspace_config(workspace)
    result = validate_standard_session(config, require_notification=require_notification)
    print(f"standard_session={str(bool(result.get('valid'))).lower()}")
    if result.get("session_id"):
        print(f"session_id={result.get('session_id')}")
    for error in result.get("errors", []):
        print(f"error={error}")
    if not result.get("valid"):
        raise SystemExit(1)


def command_validate_state(workspace: Path | None, command: str | None) -> None:
    if not command:
        raise SystemExit("缺少要校验的命令，例如 validate-state plan。")
    config = load_workspace_config(workspace)
    result = validate_st_state(config, command)
    print(f"valid={str(bool(result.get('valid'))).lower()}")
    print(f"phase={result.get('phase', '')}")
    print(f"allowed_next={','.join(str(item) for item in result.get('allowed_next', []))}")
    for error in result.get("errors", []):
        print(f"error={error}")
    if not result.get("valid"):
        raise SystemExit(1)


def command_route_st(workspace: Path | None, command_text: str | None, timeout_seconds: int, force: bool = False, output_json: bool = False) -> None:
    if not command_text:
        raise SystemExit("缺少 /st:* 命令文本。")
    parsed = parse_st_command(command_text)
    st_command = parsed["st_command"]
    run_command = parsed["run_command"]
    argument = str(parsed.get("argument", "")).strip()
    print(f"st_command={st_command}")
    print(f"run_command={run_command}")
    if argument:
        print(f"argument={argument}")
    if st_command == "/st:init":
        command_init(workspace)
        print_route_reply_constraint(st_command)
        return
    config = load_workspace_config(workspace)
    lock_path = None
    if st_command != "/st:status":
        try:
            lock_path = acquire_workflow_lock(config, st_command)
            print(f"workflow_lock={lock_path}")
        except RuntimeError as error:
            if output_json:
                print(json.dumps({"st_command": st_command, "run_command": run_command, "result": "blocked", "error": str(error)}, ensure_ascii=False, indent=2))
            raise SystemExit(str(error))
    try:
        if st_command == "/st:propose":
            command_propose_openspec(workspace, argument or None)
        elif st_command == "/st:bridge":
            command_bootstrap_openspec(workspace, explicit_st_bridge=True)
        elif st_command == "/st:plan":
            command_plan(workspace)
        elif st_command == "/st:apply":
            command_apply(workspace, timeout_seconds)
        elif st_command == "/st:review":
            command_review(workspace)
        elif st_command == "/st:verify":
            command_verify(workspace, timeout_seconds, force)
        elif st_command == "/st:archive-check":
            command_prepare_archive_openspec(workspace)
        elif st_command == "/st:archive":
            command_archive_openspec(workspace)
        elif st_command == "/st:status":
            command_status(workspace)
        else:
            raise SystemExit(f"不支持的 /st:* 命令：{st_command}")
    finally:
        release_workflow_lock(lock_path)
    print_route_reply_constraint(st_command)
    if output_json:
        state = validate_st_state(load_workspace_config(workspace), run_command)
        payload = {
            "st_command": st_command,
            "run_command": run_command,
            "argument": argument,
            "result": "ok",
            "phase": state.get("phase", ""),
            "allowed_next": state.get("allowed_next", []),
        }
        try:
            payload["session_id"] = current_session_meta(load_workspace_config(workspace)).get("session_id", "")
        except FileNotFoundError:
            payload["session_id"] = ""
        print("route_result_json_begin")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print("route_result_json_end")


def command_route_check(workspace: Path | None, command_text: str | None) -> None:
    if not command_text:
        raise SystemExit("缺少 /st:* 命令文本。")
    config = load_workspace_config(workspace)
    parsed = parse_st_command(command_text)
    st_command = parsed["st_command"]
    run_command = parsed["run_command"]
    result = validate_st_state(config, run_command)
    payload = {
        "st_command": st_command,
        "run_command": run_command,
        "argument": str(parsed.get("argument", "")).strip(),
        "workflow_source": workflow_source(config),
        "allowed": bool(result.get("valid")),
        "phase": result.get("phase", ""),
        "allowed_next": result.get("allowed_next", []),
        "errors": result.get("errors", []),
        "state_path": result.get("state_path", ""),
    }
    try:
        session = current_session_meta(config)
        payload["session_id"] = session.get("session_id", "")
    except FileNotFoundError:
        payload["session_id"] = ""
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not payload["allowed"]:
        raise SystemExit(1)


def print_route_reply_constraint(st_command: str) -> None:
    constraint = ST_ROUTE_REPLY_CONSTRAINTS.get(st_command)
    if not constraint:
        return
    print("st_reply_constraint_begin")
    for key in ("phase", "allowed_next", "forbidden_next", "final_reply_must"):
        print(f"{key}={constraint[key]}")
    print("st_reply_constraint_end")


def command_next(workspace: Path | None, timeout_seconds: int) -> None:
    config = load_workspace_config(workspace)
    session_meta = current_session_meta(config)
    status = read_json(data_artifact_path(config, "status.json", session_meta), {})
    phase = status.get("phase", "")

    if phase in ("wait_confirm_plan", "plan"):
        command_start_implement(workspace)
        return
    if phase == "implement":
        command_finish_implement(workspace)
        return
    if phase == "self_check":
        command_review(workspace)
        return
    if phase in ("wait_confirm_implement", "review"):
        command_review(workspace)
        return
    if phase == "wait_confirm_review":
        command_verify(workspace, timeout_seconds)
        return
    print(f"当前阶段无需 next：{phase}")


def command_init(workspace: Path | None) -> None:
    args = ["--workspace", str(workspace)] if workspace else []
    run_python("init-workspace.py", args)


def command_bootstrap_openspec(workspace: Path | None, *, explicit_st_bridge: bool = False) -> None:
    if not explicit_st_bridge:
        raise SystemExit(
            "拒绝执行桥接：bootstrap-openspec 只能由用户显式 /st:bridge 触发。"
            "请通过 route-st --command-text '/st:bridge' 或带 --explicit-st-bridge 的受控入口执行。"
        )
    require_st_state(load_workspace_config(workspace), "bootstrap-openspec")
    args = ["--explicit-st-bridge"]
    if workspace:
        args.extend(["--workspace", str(workspace)])
    run_python("bootstrap-openspec.py", args)


def command_propose_openspec(workspace: Path | None, change_name: str | None = None) -> None:
    args = ["--workspace", str(workspace)] if workspace else []
    if change_name:
        args.append(change_name)
    run_python("propose-openspec.py", args)
    config = load_workspace_config(workspace)
    state = read_st_state(config)
    phase = str(state.get("phase", "")).strip()
    if phase != "proposed":
        raise SystemExit(
            f"/st:propose 后状态必须停留在 proposed，当前 phase={phase}。"
            "请停止当前回复，不要生成 todo.md，不要进入 plan/apply。"
        )


def command_writeback_openspec(workspace: Path | None) -> None:
    args = ["--workspace", str(workspace)] if workspace else []
    run_python("writeback-openspec.py", args)


def command_prepare_archive_openspec(workspace: Path | None) -> None:
    require_st_state(load_workspace_config(workspace), "prepare-archive-openspec")
    args = ["--workspace", str(workspace)] if workspace else []
    run_python("prepare-archive-openspec.py", args)


def command_archive_openspec(workspace: Path | None) -> None:
    require_st_state(load_workspace_config(workspace), "archive-openspec")
    args = ["--workspace", str(workspace)] if workspace else []
    run_python("archive-openspec.py", args)


def command_plan(workspace: Path | None) -> None:
    config = load_workspace_config(workspace)
    require_st_state(config, "plan")
    try:
        active = ensure_plan_can_run(config)
    except RuntimeError as error:
        raise SystemExit(str(error))
    if active:
        session = active.get("session", {})
        session_meta = session
        if active.get("incomplete"):
            print("session_action=reused_incomplete")
            print(f"session_id={session_meta.get('session_id', '')}")
            print(f"phase={active.get('phase', '')}")
            print("next_action=复用未完成计划会话，继续生成 discovery/plan。")
        else:
            print("session_action=reused")
            print(f"session_id={session_meta.get('session_id', '')}")
            print(f"phase={active.get('phase', '')}")
            print("next_action=当前计划已存在，继续执行 /st:apply。")
            return
    else:
        command_init(workspace)
        config = load_workspace_config(workspace)
        session_meta = create_session(config)
        print("session_action=created")
        print(f"session_id={session_meta.get('session_id', '')}")
    command_discover(workspace)
    args = ["--workspace", str(workspace)] if workspace else []
    run_python("generate-smart-plan.py", args)
    update_st_state(
        config,
        phase="planned",
        last_command="/st:plan",
        artifacts={
            "todo": str(todo_path(config)),
            "plan_json": str(data_artifact_path(config, "plan.json")),
            "plan_md": str(report_artifact_path(config, "plan.md")),
        },
    )


def command_discover(workspace: Path | None) -> None:
    args = ["--workspace", str(workspace)] if workspace else []
    run_python("generate-discovery.py", args)


def command_start_implement(workspace: Path | None) -> None:
    config = load_workspace_config(workspace)
    require_st_state(config, "start-implement")
    session_meta = current_session_meta(config)
    codebases = planned_codebases(config, session_meta)
    codebase = planned_codebase(config, session_meta)
    if len(codebases) == 1:
        current_task = f"正在实现代码修改：{codebase}"
    else:
        current_task = "正在实现多仓库代码修改：" + "、".join(str(item) for item in codebases)
    update_status_for_implement(
        workspace,
        current_task=current_task,
        next_action="按 plan.json 完成代码修改，完成后执行 finish-implement。",
        phase="implement",
        progress=45,
    )
    update_st_state(config, phase="implementing", last_command="/st:apply")


def command_finish_implement(workspace: Path | None) -> None:
    config = load_workspace_config(workspace)
    require_st_state(config, "finish-implement")
    args = ["--workspace", str(workspace)] if workspace else []
    run_python("generate-self-check.py", args)
    update_st_state(
        config,
        phase="self_checked",
        last_command="/st:apply",
        artifacts={
            "self_check_json": str(data_artifact_path(config, "self-check.json")),
            "self_check_md": str(report_artifact_path(config, "self-check.md")),
        },
    )
    if config["mode"] == "manual":
        update_status_for_implement(
            workspace,
            current_task="实现阶段已完成。",
            next_action="等待确认后执行代码审查。",
            phase="wait_confirm_implement",
            progress=60,
            completed_task="已完成代码实现",
        )
        return

    update_status_for_implement(
        workspace,
        current_task="实现阶段已完成。",
        next_action="继续执行代码审查和验证。",
        phase="review",
        progress=60,
        completed_task="已完成代码实现",
    )
    command_review(workspace)
    command_verify(workspace, 300)
    config = load_workspace_config(workspace)
    verify_result = read_json(data_artifact_path(config, "verify.json"), {})
    status_result = read_json(data_artifact_path(config, "status.json"), {})
    if (
        workflow_source(config) == "openspec"
        and str(verify_result.get("result", "")).strip() == "通过"
        and str(status_result.get("phase", "")).strip() == "done"
    ):
        command_prepare_archive_openspec(workspace)


def command_review(workspace: Path | None) -> None:
    config = load_workspace_config(workspace)
    require_st_state(config, "review")
    args = ["--workspace", str(workspace)] if workspace else []
    run_python("generate-review-report.py", args)
    update_st_state(
        config,
        phase="reviewed",
        last_command="/st:review",
        artifacts={
            "review_json": str(data_artifact_path(config, "review.json")),
            "review_md": str(report_artifact_path(config, "review.md")),
        },
    )
    if workflow_source(config) == "openspec":
        run_python("writeback-openspec.py", args)


def command_verify(workspace: Path | None, timeout_seconds: int, force: bool = False) -> None:
    config = load_workspace_config(workspace)
    require_st_state(config, "verify")
    args = ["--timeout-seconds", str(timeout_seconds)]
    if force:
        args.append("--force")
    if workspace:
        args.extend(["--workspace", str(workspace)])
    run_python("run-verify-and-report.py", args)
    verify_result = read_json(data_artifact_path(config, "verify.json"), {})
    status_result = read_json(data_artifact_path(config, "status.json"), {})
    result_text = str(verify_result.get("result", "")).strip()
    status_phase = str(status_result.get("phase", "")).strip()
    next_phase = "blocked"
    if result_text == "通过" and status_phase == "done":
        next_phase = "verified" if workflow_source(config) == "openspec" else "done"
    update_st_state(
        config,
        phase=next_phase,
        last_command="/st:verify",
        artifacts={
            "verify_json": str(data_artifact_path(config, "verify.json")),
            "verify_md": str(report_artifact_path(config, "verify.md")),
            "notification_json": str(data_artifact_path(config, "notification.json")),
        },
        blocked_reason="" if next_phase in ("verified", "done") else result_text or status_result.get("current_task", "") or "验证未通过",
    )
    if workflow_source(config) == "openspec":
        run_python("writeback-openspec.py", ["--workspace", str(workspace)] if workspace else [])


def command_apply(workspace: Path | None, timeout_seconds: int) -> None:
    config = load_workspace_config(workspace)
    require_st_state(config, "apply")
    needs_plan = current_session_is_stale(config)
    if not needs_plan:
        try:
            session_meta = current_session_meta(config)
            plan_path = data_artifact_path(config, "plan.json", session_meta)
            needs_plan = not plan_path.exists()
        except FileNotFoundError:
            needs_plan = True
    if needs_plan:
        command_plan(workspace)
        config = load_workspace_config(workspace)
    if workflow_source(config) == "todo":
        command_assert_standard_session(workspace)
    command_start_implement(workspace)
    print("apply_phase=implementing")
    print("next_action=AI 必须按当前 plan.json 修改业务代码；代码完成后调用 finish-implement。")
    if config["mode"] == "auto":
        print("auto_mode=enabled")
        print("auto_note=实现代码仍需 AI 在 start-implement 与 finish-implement 之间完成，后续 self-check/review/verify 由标准脚本推进。")


def main() -> None:
    parser = argparse.ArgumentParser(description="spectrace 统一工作流入口。")
    parser.add_argument("command", choices=["route-st", "route-check", "init", "propose-openspec", "bootstrap-openspec", "writeback-openspec", "prepare-archive-openspec", "archive-openspec", "discover", "plan", "apply", "start-implement", "finish-implement", "self-check", "review", "verify", "status", "next", "validate-state", "assert-standard-session"])
    parser.add_argument("change_name", nargs="?", help="配合 propose-openspec 或 validate-state 使用。")
    parser.add_argument("--command-text", help="配合 route-st 使用，传入完整 /st:* 命令文本。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--force", action="store_true", help="配合 verify 使用，强制重跑验证并覆盖结果。")
    parser.add_argument("--json", action="store_true", help="输出机器可读摘要。")
    parser.add_argument("--explicit-st-bridge", action="store_true", help="确认本次 bootstrap-openspec 来自用户显式 /st:bridge 命令。")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser() if args.workspace else None

    if args.command == "route-st":
        command_route_st(workspace, args.command_text or args.change_name, args.timeout_seconds, args.force, args.json)
    elif args.command == "route-check":
        command_route_check(workspace, args.command_text or args.change_name)
    elif args.command == "init":
        command_init(workspace)
    elif args.command == "propose-openspec":
        command_propose_openspec(workspace, args.change_name)
    elif args.command == "bootstrap-openspec":
        command_bootstrap_openspec(workspace, explicit_st_bridge=args.explicit_st_bridge)
    elif args.command == "writeback-openspec":
        command_writeback_openspec(workspace)
    elif args.command == "prepare-archive-openspec":
        command_prepare_archive_openspec(workspace)
    elif args.command == "archive-openspec":
        command_archive_openspec(workspace)
    elif args.command == "discover":
        command_discover(workspace)
    elif args.command == "plan":
        command_plan(workspace)
    elif args.command == "apply":
        command_apply(workspace, args.timeout_seconds)
    elif args.command == "start-implement":
        command_start_implement(workspace)
    elif args.command == "finish-implement":
        command_finish_implement(workspace)
    elif args.command == "self-check":
        run_python("generate-self-check.py", ["--workspace", str(workspace)] if workspace else [])
    elif args.command == "review":
        command_review(workspace)
    elif args.command == "verify":
        command_verify(workspace, args.timeout_seconds, args.force)
    elif args.command == "status":
        command_status(workspace)
    elif args.command == "next":
        command_next(workspace, args.timeout_seconds)
    elif args.command == "validate-state":
        command_validate_state(workspace, args.change_name)
    elif args.command == "assert-standard-session":
        command_assert_standard_session(workspace, require_notification=args.force)


if __name__ == "__main__":
    main()
