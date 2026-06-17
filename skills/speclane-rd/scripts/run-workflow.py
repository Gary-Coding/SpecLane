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
    active_demand_path,
    acquire_workflow_lock,
    demand_instance_path,
    demand_registry_dir,
    demand_runtime_dir,
    ensure_plan_can_run,
    ensure_status,
    load_workspace_config,
    now_iso,
    parse_sl_command,
    parse_simple_yaml,
    planned_codebases,
    planned_codebase,
    read_json,
    read_sl_state,
    release_workflow_lock,
    require_sl_state,
    report_artifact_path,
    recover_workflow_state_from_artifacts,
    recover_sl_state_from_artifacts,
    todo_path,
    update_sl_state,
    validate_standard_session,
    validate_sl_state,
    validate_demand_name,
    workflow_source,
    write_managed_json,
    write_active_demand,
    workspace_root,
)


SCRIPT_DIR = Path(__file__).resolve().parent


SL_ROUTE_REPLY_CONSTRAINTS: dict[str, dict[str, str]] = {
    "/sl:propose": {
        "phase": "proposed",
        "allowed_next": "/sl:bridge",
        "forbidden_next": "/sl:plan,/sl:apply",
        "final_reply_must": "代码未修改。下一步只能执行 /sl:bridge。",
    },
    "/sl:bridge": {
        "phase": "bridged",
        "allowed_next": "人工审核 todo.md 后 /sl:apply",
        "forbidden_next": "自动执行 /sl:plan,自动执行 /sl:apply,代码实现",
        "final_reply_must": "桥接 todo 已生成。请审核 todo.md，审核通过后发送 /sl:apply。",
    },
    "/sl:plan": {
        "phase": "planned",
        "allowed_next": "/sl:apply",
        "forbidden_next": "代码实现,review,verify",
        "final_reply_must": "计划已生成。下一步执行 /sl:apply。",
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


def scoped_workspace_args(workspace: Path | None) -> list[str]:
    args = ["--workspace", str(workspace)] if workspace else []
    demand_name = current_command_demand()
    if demand_name:
        args.extend(["--demand", demand_name])
    return args


def current_command_demand() -> str:
    import os

    return str(os.environ.get("SPECLANE_DEMAND_NAME", "")).strip()


def set_command_demand(demand_name: str) -> None:
    import os

    if demand_name:
        os.environ["SPECLANE_DEMAND_NAME"] = validate_demand_name(demand_name)


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
    recover_sl_state_from_artifacts(config)
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
    state = read_sl_state(config)
    if state:
        print(f"sl_phase={state.get('phase', '')}")
        print(f"sl_allowed_next={','.join(str(item) for item in state.get('allowed_next', []))}")
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
    result = validate_sl_state(config, command)
    print(f"valid={str(bool(result.get('valid'))).lower()}")
    print(f"phase={result.get('phase', '')}")
    print(f"allowed_next={','.join(str(item) for item in result.get('allowed_next', []))}")
    for error in result.get("errors", []):
        print(f"error={error}")
    if not result.get("valid"):
        raise SystemExit(1)


def command_route_st(workspace: Path | None, command_text: str | None, timeout_seconds: int, force: bool = False, output_json: bool = False) -> None:
    if not command_text:
        raise SystemExit("缺少 /sl:* 命令文本。")
    parsed = parse_sl_command(command_text)
    sl_command = parsed["sl_command"]
    run_command = parsed["run_command"]
    argument = str(parsed.get("argument", "")).strip()
    demand_name = str(parsed.get("demand_name", "")).strip()
    if demand_name:
        set_command_demand(demand_name)
    print(f"sl_command={sl_command}")
    print(f"run_command={run_command}")
    if argument:
        print(f"argument={argument}")
    if demand_name:
        print(f"demand_name={demand_name}")
    if sl_command == "/sl:demand":
        command_demand(workspace, argument, str(parsed.get("raw_text", "")))
        return
    if sl_command == "/sl:init":
        command_init(workspace)
        print_route_reply_constraint(sl_command)
        return
    config = load_workspace_config(workspace)
    preflight = validate_sl_state(config, run_command)
    if not preflight.get("valid"):
        payload = {
            "sl_command": sl_command,
            "run_command": run_command,
            "result": "blocked",
            "phase": preflight.get("phase", ""),
            "allowed_next": preflight.get("allowed_next", []),
            "errors": preflight.get("errors", []),
            "next_action": "请按 allowed_next 或错误提示继续；如状态异常，先执行 /sl:recover。",
        }
        if output_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("route_guard=blocked")
            print(f"phase={payload['phase']}")
            print(f"allowed_next={','.join(str(item) for item in payload['allowed_next'])}")
            for item in payload["errors"]:
                print(f"error={item}")
            print(f"next_action={payload['next_action']}")
        raise SystemExit(1)
    lock_path = None
    if sl_command != "/sl:status":
        try:
            lock_path = acquire_workflow_lock(config, sl_command)
            print(f"workflow_lock={lock_path}")
        except RuntimeError as error:
            if output_json:
                print(json.dumps({"sl_command": sl_command, "run_command": run_command, "result": "blocked", "error": str(error)}, ensure_ascii=False, indent=2))
            raise SystemExit(str(error))
    try:
        if sl_command == "/sl:propose":
            command_propose_openspec(workspace, argument or None)
        elif sl_command == "/sl:bridge":
            command_bootstrap_openspec(workspace, explicit_sl_bridge=True)
        elif sl_command == "/sl:plan":
            command_plan(workspace)
        elif sl_command == "/sl:apply":
            command_apply(workspace, timeout_seconds)
        elif sl_command == "/sl:review":
            command_review(workspace)
        elif sl_command == "/sl:verify":
            command_verify(workspace, timeout_seconds, force)
        elif sl_command == "/sl:archive-check":
            command_prepare_archive_openspec(workspace)
        elif sl_command == "/sl:archive":
            command_archive_openspec(workspace)
        elif sl_command == "/sl:status":
            command_status(workspace)
        elif sl_command == "/sl:recover":
            command_recover(workspace)
        else:
            raise SystemExit(f"不支持的 /sl:* 命令：{sl_command}")
    finally:
        release_workflow_lock(lock_path)
    print_route_reply_constraint(sl_command)
    if output_json:
        state = validate_sl_state(load_workspace_config(workspace), run_command)
        payload = {
            "sl_command": sl_command,
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
        raise SystemExit("缺少 /sl:* 命令文本。")
    parsed = parse_sl_command(command_text)
    sl_command = parsed["sl_command"]
    run_command = parsed["run_command"]
    demand_name = str(parsed.get("demand_name", "")).strip()
    if demand_name:
        set_command_demand(demand_name)
    config = load_workspace_config(workspace)
    result = validate_sl_state(config, run_command)
    payload = {
        "sl_command": sl_command,
        "run_command": run_command,
        "argument": str(parsed.get("argument", "")).strip(),
        "demand_name": demand_name,
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


def _demand_config_text(demand_name: str, workflow_source_value: str, mode: str, change_name: str = "") -> str:
    lines = [
        "version: 1",
        f"demand_name: {demand_name}",
        f"workflow_source: {workflow_source_value}",
        f"mode: {mode}",
        f"demand_file: demands/{demand_name}/需求.md",
        f"todo_file: demands/{demand_name}/todo.md",
        f"output_dir: demands/{demand_name}/output",
        "reference_files: []",
    ]
    if change_name:
        lines.extend(["openspec:", f"  change_name: {change_name}"])
    return "\n".join(lines) + "\n"


def command_demand(workspace: Path | None, action: str, raw_text: str) -> None:
    root = workspace_root(workspace)
    action = action or "list"
    parts = raw_text.split()
    demand_arg = ""
    if len(parts) >= 3 and not parts[2].startswith("--"):
        demand_arg = parts[2]
    if action in ("new", "use", "status") and not demand_arg:
        raise SystemExit(f"/sl:demand {action} 需要需求名称。")

    if action == "new":
        demand_name = validate_demand_name(demand_arg)
        demand_dir = root / "demands" / demand_name
        demand_dir.mkdir(parents=True, exist_ok=True)
        demand_runtime_dir(root, demand_name).mkdir(parents=True, exist_ok=True)
        demand_file = demand_dir / "需求.md"
        todo_file = demand_dir / "todo.md"
        if not demand_file.exists():
            demand_file.write_text("# 需求说明\n\n## 背景\n\n## 目标\n\n## 验收标准\n\n- [ ] 补充验收标准。\n", encoding="utf-8")
        if not todo_file.exists():
            todo_file.write_text("# 待办事项\n\n- [ ] 根据需求补充任务。\n", encoding="utf-8")
        instance = demand_instance_path(root, demand_name)
        if not instance.exists():
            instance.write_text(_demand_config_text(demand_name, "openspec", "auto"), encoding="utf-8")
        write_active_demand(root, demand_name)
        print("demand_action=new")
        print(f"demand_name={demand_name}")
        print(f"demand_file={demand_file}")
        print(f"todo_file={todo_file}")
        print(f"demand_config={instance}")
        print("active_demand=updated")
        return

    if action == "use":
        demand_name = validate_demand_name(demand_arg)
        if not demand_instance_path(root, demand_name).exists():
            raise SystemExit(f"需求实例不存在：{demand_instance_path(root, demand_name)}。请先执行 /sl:demand new {demand_name}。")
        write_active_demand(root, demand_name)
        print("demand_action=use")
        print(f"demand_name={demand_name}")
        print("active_demand=updated")
        return

    if action == "list":
        print("demand_action=list")
        active = ""
        if active_demand_path(root).exists():
            try:
                active_data = parse_simple_yaml(active_demand_path(root).read_text(encoding="utf-8"))
                active = str(active_data.get("demand_name", "")) if isinstance(active_data, dict) else ""
            except Exception:
                active = ""
        print(f"active_demand={active}")
        registry = demand_registry_dir(root)
        if not registry.exists():
            print("demands=")
            return
        names = [path.name for path in sorted(registry.iterdir()) if (path / "demand.yml").exists()]
        print("demands=" + ",".join(names))
        for name in names:
            print(f"demand.{name}.config={demand_instance_path(root, name)}")
        return

    if action == "status":
        demand_name = validate_demand_name(demand_arg)
        set_command_demand(demand_name)
        print("demand_action=status")
        print(f"demand_name={demand_name}")
        command_status(workspace)
        return

    raise SystemExit("不支持的 /sl:demand 操作。支持：new/use/list/status。")


def command_recover(workspace: Path | None) -> None:
    config = load_workspace_config(workspace)
    state = recover_workflow_state_from_artifacts(config)
    print("recover_result=ok")
    print(f"phase={state.get('phase', '')}")
    print(f"allowed_next={','.join(str(item) for item in state.get('allowed_next', []))}")
    if state.get("blocked_reason"):
        print(f"blocked_reason={state.get('blocked_reason')}")
    artifacts = state.get("artifacts", {})
    if isinstance(artifacts, dict):
        for key in sorted(artifacts):
            print(f"artifact.{key}={artifacts[key]}")


def print_route_reply_constraint(sl_command: str) -> None:
    constraint = SL_ROUTE_REPLY_CONSTRAINTS.get(sl_command)
    if not constraint:
        return
    print("sl_reply_constraint_begin")
    for key in ("phase", "allowed_next", "forbidden_next", "final_reply_must"):
        print(f"{key}={constraint[key]}")
    print("sl_reply_constraint_end")


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
    args = scoped_workspace_args(workspace)
    run_python("init-workspace.py", args)


def command_bootstrap_openspec(workspace: Path | None, *, explicit_sl_bridge: bool = False) -> None:
    if not explicit_sl_bridge:
        raise SystemExit(
            "拒绝执行桥接：bootstrap-openspec 只能由用户显式 /sl:bridge 触发。"
            "请通过 route-sl --command-text '/sl:bridge' 或带 --explicit-sl-bridge 的受控入口执行。"
        )
    require_sl_state(load_workspace_config(workspace), "bootstrap-openspec")
    args = ["--explicit-sl-bridge"]
    args.extend(scoped_workspace_args(workspace))
    run_python("bootstrap-openspec.py", args)


def command_propose_openspec(workspace: Path | None, change_name: str | None = None) -> None:
    args = scoped_workspace_args(workspace)
    if change_name:
        args.append(change_name)
    run_python("propose-openspec.py", args)
    config = load_workspace_config(workspace)
    state = read_sl_state(config)
    phase = str(state.get("phase", "")).strip()
    if phase != "proposed":
        raise SystemExit(
            f"/sl:propose 后状态必须停留在 proposed，当前 phase={phase}。"
            "请停止当前回复，不要生成 todo.md，不要进入 plan/apply。"
        )


def command_writeback_openspec(workspace: Path | None) -> None:
    args = scoped_workspace_args(workspace)
    run_python("writeback-openspec.py", args)


def command_prepare_archive_openspec(workspace: Path | None) -> None:
    require_sl_state(load_workspace_config(workspace), "prepare-archive-openspec")
    args = scoped_workspace_args(workspace)
    run_python("prepare-archive-openspec.py", args)


def command_archive_openspec(workspace: Path | None) -> None:
    require_sl_state(load_workspace_config(workspace), "archive-openspec")
    args = scoped_workspace_args(workspace)
    run_python("archive-openspec.py", args)


def command_plan(workspace: Path | None) -> None:
    config = load_workspace_config(workspace)
    require_sl_state(config, "plan")
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
            print("next_action=当前计划已存在，继续执行 /sl:apply。")
            return
    else:
        command_init(workspace)
        config = load_workspace_config(workspace)
        session_meta = create_session(config)
        print("session_action=created")
        print(f"session_id={session_meta.get('session_id', '')}")
    command_discover(workspace)
    args = scoped_workspace_args(workspace)
    run_python("generate-smart-plan.py", args)
    update_sl_state(
        config,
        phase="planned",
        lasl_command="/sl:plan",
        artifacts={
            "todo": str(todo_path(config)),
            "plan_json": str(data_artifact_path(config, "plan.json")),
            "plan_md": str(report_artifact_path(config, "plan.md")),
        },
    )


def command_discover(workspace: Path | None) -> None:
    args = scoped_workspace_args(workspace)
    run_python("generate-discovery.py", args)


def command_start_implement(workspace: Path | None) -> None:
    config = load_workspace_config(workspace)
    require_sl_state(config, "start-implement")
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
    update_sl_state(config, phase="implementing", lasl_command="/sl:apply")


def command_finish_implement(workspace: Path | None) -> None:
    config = load_workspace_config(workspace)
    require_sl_state(config, "finish-implement")
    args = scoped_workspace_args(workspace)
    run_python("generate-self-check.py", args)
    update_sl_state(
        config,
        phase="self_checked",
        lasl_command="/sl:apply",
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
    require_sl_state(config, "review")
    args = scoped_workspace_args(workspace)
    run_python("generate-review-report.py", args)
    update_sl_state(
        config,
        phase="reviewed",
        lasl_command="/sl:review",
        artifacts={
            "review_json": str(data_artifact_path(config, "review.json")),
            "review_md": str(report_artifact_path(config, "review.md")),
        },
    )
    if workflow_source(config) == "openspec":
        run_python("writeback-openspec.py", args)


def command_verify(workspace: Path | None, timeout_seconds: int, force: bool = False) -> None:
    config = load_workspace_config(workspace)
    require_sl_state(config, "verify")
    args = ["--timeout-seconds", str(timeout_seconds)]
    if force:
        args.append("--force")
    args.extend(scoped_workspace_args(workspace))
    run_python("run-verify-and-report.py", args)
    verify_result = read_json(data_artifact_path(config, "verify.json"), {})
    status_result = read_json(data_artifact_path(config, "status.json"), {})
    result_text = str(verify_result.get("result", "")).strip()
    status_phase = str(status_result.get("phase", "")).strip()
    next_phase = "blocked"
    if result_text == "通过" and status_phase == "done":
        next_phase = "verified" if workflow_source(config) == "openspec" else "done"
    update_sl_state(
        config,
        phase=next_phase,
        lasl_command="/sl:verify",
        artifacts={
            "verify_json": str(data_artifact_path(config, "verify.json")),
            "verify_md": str(report_artifact_path(config, "verify.md")),
            "notification_json": str(data_artifact_path(config, "notification.json")),
        },
        blocked_reason="" if next_phase in ("verified", "done") else result_text or status_result.get("current_task", "") or "验证未通过",
    )
    if workflow_source(config) == "openspec":
        run_python("writeback-openspec.py", scoped_workspace_args(workspace))


def command_apply(workspace: Path | None, timeout_seconds: int) -> None:
    config = load_workspace_config(workspace)
    require_sl_state(config, "apply")
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
    parser = argparse.ArgumentParser(description="speclane 统一工作流入口。")
    parser.add_argument("command", choices=["route-sl", "route-check", "init", "propose-openspec", "bootstrap-openspec", "writeback-openspec", "prepare-archive-openspec", "archive-openspec", "discover", "plan", "apply", "start-implement", "finish-implement", "self-check", "review", "verify", "status", "recover", "next", "validate-state", "assert-standard-session"])
    parser.add_argument("change_name", nargs="?", help="配合 propose-openspec 或 validate-state 使用。")
    parser.add_argument("--command-text", help="配合 route-sl 使用，传入完整 /sl:* 命令文本。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    parser.add_argument("--demand", help="需求实例名称，用于多需求状态隔离。")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--force", action="store_true", help="配合 verify 使用，强制重跑验证并覆盖结果。")
    parser.add_argument("--json", action="store_true", help="输出机器可读摘要。")
    parser.add_argument("--explicit-sl-bridge", action="store_true", help="确认本次 bootstrap-openspec 来自用户显式 /sl:bridge 命令。")
    args = parser.parse_args()
    if getattr(args, "demand", None):
        import os

        os.environ["SPECLANE_DEMAND_NAME"] = args.demand

    workspace = Path(args.workspace).expanduser() if args.workspace else None

    if args.command == "route-sl":
        command_route_st(workspace, args.command_text or args.change_name, args.timeout_seconds, args.force, args.json)
    elif args.command == "route-check":
        command_route_check(workspace, args.command_text or args.change_name)
    elif args.command == "init":
        command_init(workspace)
    elif args.command == "propose-openspec":
        command_propose_openspec(workspace, args.change_name)
    elif args.command == "bootstrap-openspec":
        command_bootstrap_openspec(workspace, explicit_sl_bridge=args.explicit_sl_bridge)
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
        run_python("generate-self-check.py", scoped_workspace_args(workspace))
    elif args.command == "review":
        command_review(workspace)
    elif args.command == "verify":
        command_verify(workspace, args.timeout_seconds, args.force)
    elif args.command == "status":
        command_status(workspace)
    elif args.command == "recover":
        command_recover(workspace)
    elif args.command == "next":
        command_next(workspace, args.timeout_seconds)
    elif args.command == "validate-state":
        command_validate_state(workspace, args.change_name)
    elif args.command == "assert-standard-session":
        command_assert_standard_session(workspace, require_notification=args.force)


if __name__ == "__main__":
    main()
