from __future__ import annotations

from pathlib import Path
from typing import Any

from .io_utils import read_json, read_text, write_json
from .notify import feishu_config, is_standard_workflow_notification, pushplus_config
from .openspec import openspec_change_dir, openspec_change_name, openspec_tasks_hash, openspec_tasks_path
from .project_detect import todo_path
from .session import (
    current_session_is_stale,
    current_session_meta,
    data_artifact_path,
    report_artifact_path,
    sl_state_path,
    todo_state_path,
    workflow_source,
    workflow_state_path,
)
from .time_utils import now_iso


def default_status(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "session_id": "",
        "data_dir": "",
        "report_dir": "",
        "phase": "context",
        "current_task": "等待开始工作流。",
        "progress": 0,
        "awaiting_confirmation": False,
        "pending_confirmation_for": "",
        "next_action": "读取 todo 文件并生成计划。",
        "completed_tasks": [],
        "blocked_tasks": [],
        "started_at": "",
        "finished_at": "",
        "duration_seconds": 0,
        "notification_status": "pending",
        "notification_message": "",
        "updated_at": now_iso(),
    }


def ensure_status(config: dict[str, Any], session_meta: dict[str, Any], status: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = default_status(config["mode"])
    if status:
        merged.update(status)
    merged["mode"] = config["mode"]
    merged["session_id"] = session_meta["session_id"]
    merged["data_dir"] = session_meta["data_dir"]
    merged["report_dir"] = session_meta["report_dir"]
    merged["started_at"] = (
        str(merged.get("started_at", "")).strip()
        or str(session_meta.get("started_at", "")).strip()
        or str(session_meta.get("created_at", "")).strip()
        or now_iso()
    )
    merged["finished_at"] = str(merged.get("finished_at", "")).strip()
    try:
        merged["duration_seconds"] = float(merged.get("duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        merged["duration_seconds"] = 0.0
    merged["notification_status"] = str(merged.get("notification_status", "pending") or "pending")
    merged["notification_message"] = str(merged.get("notification_message", "") or "")
    return merged


def _is_todo_template_placeholder(todo_text: str) -> bool:
    normalized = todo_text.strip()
    if not normalized:
        return True
    markers = [
        "your-service-name",
        "在这里写大需求模块名称",
        "在这里写主任务 1",
        "在这里写需要补充的测试、文档或验证要求",
    ]
    return any(marker in normalized for marker in markers)

SL_PHASE_ALLOWED_NEXT: dict[str, list[str]] = {
    "draft": ["/sl:propose"],
    "proposed": ["/sl:bridge"],
    "bridged": ["/sl:apply", "/sl:plan"],
    "planned": ["/sl:apply"],
    "implementing": [],
    "self_checked": ["/sl:review"],
    "reviewed": ["/sl:verify", "/sl:apply"],
    "verified": ["/sl:archive-check"],
    "archive_ready": ["/sl:archive"],
    "archived": [],
    "blocked": ["/sl:apply", "/sl:verify"],
}

TODO_PHASE_ALLOWED_NEXT: dict[str, list[str]] = {
    "draft": ["/sl:init", "/sl:plan", "/sl:apply"],
    "planned": ["/sl:apply"],
    "implementing": [],
    "self_checked": ["/sl:review"],
    "reviewed": ["/sl:verify", "/sl:apply"],
    "done": [],
    "blocked": ["/sl:apply", "/sl:verify"],
}


RUN_COMMAND_TO_SL_COMMAND: dict[str, str] = {
    "route-sl": "",
    "openspec-propose": "/sl:propose",
    "openspec-bridge": "/sl:bridge",
    "plan": "/sl:plan",
    "apply": "/sl:apply",
    "start-implement": "/sl:apply",
    "finish-implement": "/sl:apply",
    "review": "/sl:review",
    "verify": "/sl:verify",
    "openspec-archive-check": "/sl:archive-check",
    "openspec-archive": "/sl:archive",
    "qa-plan": "/sl:qa:plan",
    "qa-report": "/sl:qa:report",
}


SL_COMMAND_TO_RUN_COMMAND: dict[str, str] = {
    "/sl:init": "init",
    "/sl:propose": "openspec-propose",
    "/sl:bridge": "openspec-bridge",
    "/sl:plan": "plan",
    "/sl:apply": "apply",
    "/sl:review": "review",
    "/sl:verify": "verify",
    "/sl:archive-check": "openspec-archive-check",
    "/sl:archive": "openspec-archive",
    "/sl:status": "status",
    "/sl:recover": "recover",
    "/sl:demand": "demand",
    "/sl:qa:plan": "qa-plan",
    "/sl:qa:report": "qa-report",
}


def read_sl_state(config: dict[str, Any]) -> dict[str, Any]:
    state = read_json(workflow_state_path(config), {})
    if not isinstance(state, dict):
        state = {}
    phase = str(state.get("phase", "") or "").strip() or "draft"
    allowed_next = state.get("allowed_next")
    if not isinstance(allowed_next, list):
        allowed_map = TODO_PHASE_ALLOWED_NEXT if workflow_source(config) == "todo" else SL_PHASE_ALLOWED_NEXT
        allowed_next = allowed_map.get(phase, [])
    state["phase"] = phase
    state["allowed_next"] = [str(item) for item in allowed_next]
    return state


def write_sl_state(config: dict[str, Any], state: dict[str, Any]) -> Path:
    phase = str(state.get("phase", "") or "draft").strip()
    state["phase"] = phase
    allowed_map = TODO_PHASE_ALLOWED_NEXT if workflow_source(config) == "todo" else SL_PHASE_ALLOWED_NEXT
    state["allowed_next"] = list(allowed_map.get(phase, []))
    state["updated_at"] = now_iso()
    path = workflow_state_path(config)
    write_json(path, state)
    return path


def update_sl_state(
    config: dict[str, Any],
    phase: str,
    lasl_command: str,
    artifacts: dict[str, Any] | None = None,
    blocked_reason: str = "",
) -> dict[str, Any]:
    state = read_sl_state(config)
    state.update(
        {
            "phase": phase,
            "last_command": lasl_command,
            "lasl_command": lasl_command,
            "current_change": openspec_change_name(config) if workflow_source(config) == "openspec" else "",
            "blocked_reason": blocked_reason,
        }
    )
    if artifacts:
        existing_artifacts = state.get("artifacts", {})
        if not isinstance(existing_artifacts, dict):
            existing_artifacts = {}
        existing_artifacts.update(artifacts)
        state["artifacts"] = existing_artifacts
    write_sl_state(config, state)
    return state


def _sl_state_artifact_exists(path_text: str) -> bool:
    return bool(path_text and Path(path_text).exists())


def _safe_current_session_meta(config: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return current_session_meta(config)
    except FileNotFoundError:
        return None


def _status_phase_for_todo(config: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    if current_session_is_stale(config):
        return "draft", {}, None
    session_meta = _safe_current_session_meta(config)
    if not session_meta:
        return "draft", {}, None
    status_path = data_artifact_path(config, "status.json", session_meta)
    status = read_json(status_path, {})
    if not isinstance(status, dict) or not status:
        if data_artifact_path(config, "plan.json", session_meta).exists():
            return "planned", {}, session_meta
        return "draft", {}, session_meta
    status_phase = str(status.get("phase", "") or "").strip()
    phase_map = {
        "context": "draft",
        "plan": "planned",
        "wait_confirm_plan": "planned",
        "implement": "implementing",
        "self_check": "self_checked",
        "wait_confirm_implement": "self_checked",
        "review": "reviewed",
        "wait_confirm_review": "reviewed",
        "done": "done",
        "blocked": "blocked",
    }
    return phase_map.get(status_phase, status_phase or "draft"), status, session_meta


def _standard_source(payload: dict[str, Any], expected: str) -> bool:
    return str(payload.get("source", "")).strip() == expected


def validate_standard_session(config: dict[str, Any], require_notification: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    if current_session_is_stale(config):
        errors.append("current-session.json 指向旧 output_dir，请重新执行 /sl:plan 创建当前需求的标准会话。")
        return {"valid": False, "errors": errors}
    session_meta = _safe_current_session_meta(config)
    if not session_meta:
        errors.append("缺少当前 session，请先执行 /sl:plan。")
        return {"valid": False, "errors": errors}

    status_path = data_artifact_path(config, "status.json", session_meta)
    status = read_json(status_path, {})
    if not isinstance(status, dict) or not status:
        errors.append("缺少标准 status.json。")
        status = {}

    plan = read_json(data_artifact_path(config, "plan.json", session_meta), {})
    if not isinstance(plan, dict) or not plan:
        errors.append("缺少标准 plan.json。")
    elif not _standard_source(plan, "run-workflow.py plan"):
        errors.append("plan.json 不是标准脚本生成的产物。")

    optional_sources = {
        "self-check.json": "run-workflow.py self-check",
        "review.json": "run-workflow.py review",
        "verify.json": "run-workflow.py verify",
    }
    for artifact_name, source in optional_sources.items():
        path = data_artifact_path(config, artifact_name, session_meta)
        if not path.exists():
            continue
        payload = read_json(path, {})
        if not isinstance(payload, dict) or not _standard_source(payload, source):
            errors.append(f"{artifact_name} 不是标准脚本生成的产物。")

    if require_notification or str(status.get("phase", "")).strip() == "done":
        verify = read_json(data_artifact_path(config, "verify.json", session_meta), {})
        notification = read_json(data_artifact_path(config, "notification.json", session_meta), {})
        overall_result = str(verify.get("result", "")).strip() if isinstance(verify, dict) else ""
        if overall_result != "通过":
            errors.append("缺少通过状态的标准 verify.json。")
        else:
            pushplus_enabled = any(item.get("enabled") for item in pushplus_config(config).get("routes", []))
            notification_enabled = bool(feishu_config(config).get("enabled") or pushplus_enabled)
            if notification_enabled:
                if not is_standard_workflow_notification(config, session_meta, ensure_status(config, session_meta, status), overall_result, notification):
                    errors.append("缺少标准 notification.json，或通知不是由 run-workflow.py verify 成功发送。")
            elif not (
                isinstance(notification, dict)
                and str(notification.get("provider", "")).strip() == "notification"
                and str(notification.get("source", "")).strip() == "run-workflow.py verify"
                and str(notification.get("status", "")).strip() == "skipped"
            ):
                errors.append("缺少标准 notification.json，或未按未配置通知场景标记 skipped。")

    return {
        "valid": not errors,
        "errors": errors,
        "session_id": session_meta["session_id"] if session_meta else "",
    }


def recover_sl_state_from_artifacts(config: dict[str, Any]) -> dict[str, Any]:
    if workflow_source(config) != "openspec":
        return {"phase": "", "allowed_next": []}
    state = read_sl_state(config)
    if sl_state_path(config).exists() and str(state.get("phase", "")).strip() != "draft":
        return state

    artifacts = dict(state.get("artifacts", {}) if isinstance(state.get("artifacts"), dict) else {})
    phase = "draft"
    lasl_command = ""
    try:
        proposal = openspec_change_dir(config) / "proposal.md"
        design = openspec_change_dir(config) / "design.md"
        tasks = openspec_tasks_path(config)
        if proposal.exists() or design.exists() or tasks.exists():
            artifacts.update(
                {
                    "proposal": str(proposal),
                    "design": str(design),
                    "tasks": str(tasks),
                    "change_dir": str(openspec_change_dir(config)),
                }
            )
        if proposal.exists() and design.exists() and tasks.exists():
            phase = "proposed"
            lasl_command = "/sl:propose"
    except Exception:
        pass

    try:
        todo_file = todo_path(config)
        todo_text = read_text(todo_file)
        if todo_file.exists() and not _is_todo_template_placeholder(todo_text):
            artifacts["todo"] = str(todo_file)
            phase = "bridged"
            lasl_command = "/sl:bridge"
    except Exception:
        pass

    try:
        session_meta = current_session_meta(config)
        plan_path = data_artifact_path(config, "plan.json", session_meta)
        self_check_path = data_artifact_path(config, "self-check.json", session_meta)
        review_path = data_artifact_path(config, "review.json", session_meta)
        verify_path = data_artifact_path(config, "verify.json", session_meta)
        notification_path = data_artifact_path(config, "notification.json", session_meta)
        if plan_path.exists():
            artifacts["plan_json"] = str(plan_path)
            artifacts["plan_md"] = str(report_artifact_path(config, "plan.md", session_meta))
            phase = "planned"
            lasl_command = "/sl:plan"
        if self_check_path.exists():
            artifacts["self_check_json"] = str(self_check_path)
            artifacts["self_check_md"] = str(report_artifact_path(config, "self-check.md", session_meta))
            phase = "self_checked"
            lasl_command = "/sl:apply"
        if review_path.exists():
            artifacts["review_json"] = str(review_path)
            artifacts["review_md"] = str(report_artifact_path(config, "review.md", session_meta))
            phase = "reviewed"
            lasl_command = "/sl:review"
        if verify_path.exists():
            artifacts["verify_json"] = str(verify_path)
            artifacts["verify_md"] = str(report_artifact_path(config, "verify.md", session_meta))
            artifacts["notification_json"] = str(notification_path)
            verify = read_json(verify_path, {})
            status = ensure_status(config, session_meta, read_json(data_artifact_path(config, "status.json", session_meta), {}))
            notification = read_json(notification_path, {})
            overall_result = str(verify.get("result") or verify.get("overall_result") or "").strip()
            if overall_result == "通过" and is_standard_workflow_notification(config, session_meta, status, overall_result, notification):
                phase = "verified"
                lasl_command = "/sl:verify"
            else:
                phase = "reviewed"
                lasl_command = "/sl:review"
    except Exception:
        pass

    if phase != "draft":
        state.update(
            {
                "phase": phase,
                "last_command": lasl_command,
                "lasl_command": lasl_command,
                "current_change": openspec_change_name(config),
                "blocked_reason": "",
                "artifacts": artifacts,
            }
        )
        write_sl_state(config, state)
    return read_sl_state(config)


def recover_todo_state_from_artifacts(config: dict[str, Any]) -> dict[str, Any]:
    phase, status, session_meta = _status_phase_for_todo(config)
    artifacts: dict[str, Any] = {}
    if session_meta:
        artifact_names = {
            "plan_json": "plan.json",
            "self_check_json": "self-check.json",
            "review_json": "review.json",
            "verify_json": "verify.json",
            "notification_json": "notification.json",
        }
        report_names = {
            "plan_md": "plan.md",
            "self_check_md": "self-check.md",
            "review_md": "review.md",
            "verify_md": "verify.md",
        }
        for key, name in artifact_names.items():
            path = data_artifact_path(config, name, session_meta)
            if path.exists():
                artifacts[key] = str(path)
        for key, name in report_names.items():
            path = report_artifact_path(config, name, session_meta)
            if path.exists():
                artifacts[key] = str(path)
    state = read_sl_state(config)
    state.update(
        {
            "phase": phase,
            "last_command": "",
            "lasl_command": "",
            "current_change": "",
            "blocked_reason": str(status.get("current_task", "")) if phase == "blocked" and isinstance(status, dict) else "",
            "artifacts": artifacts,
        }
    )
    write_sl_state(config, state)
    return read_sl_state(config)


def recover_workflow_state_from_artifacts(config: dict[str, Any]) -> dict[str, Any]:
    if workflow_source(config) == "openspec":
        return recover_sl_state_from_artifacts(config)
    return recover_todo_state_from_artifacts(config)


def validate_sl_state(config: dict[str, Any], run_command: str) -> dict[str, Any]:
    if run_command in ("qa-plan", "qa-report"):
        state = recover_workflow_state_from_artifacts(config)
        return {
            "valid": True,
            "phase": state.get("phase", ""),
            "allowed_next": state.get("allowed_next", []),
            "warnings": ["QA 命令为旁路阶段，不改变 RD 状态机。"],
        }
    if workflow_source(config) != "openspec":
        sl_command = RUN_COMMAND_TO_SL_COMMAND.get(run_command, "")
        phase, status, session_meta = _status_phase_for_todo(config)
        allowed_next = TODO_PHASE_ALLOWED_NEXT.get(phase, [])
        errors: list[str] = []
        if run_command in ("openspec-propose", "openspec-bridge", "openspec-archive-check", "openspec-archive"):
            errors.append("当前是 todo 模式，不能执行 OpenSpec 专属命令。")
        elif run_command in ("plan", "apply"):
            if not todo_path(config).exists():
                errors.append("缺少 todo_file，请先执行 /sl:init 或补充 todo.md。")
            if run_command == "plan" and phase in ("implementing", "self_checked", "reviewed"):
                errors.append("当前已有活跃 session 正在交付中，不能重新执行 /sl:plan。请继续当前 /sl:apply、/sl:review 或 /sl:verify。")
        elif run_command == "start-implement":
            if phase not in ("planned", "implementing", "blocked"):
                errors.append("当前状态不允许进入实现，请先执行 /sl:plan。")
        elif run_command == "finish-implement":
            if phase != "implementing":
                errors.append("当前状态不允许完成实现，必须先通过 /sl:apply 进入 implementing。")
        elif run_command == "review":
            if phase not in ("self_checked", "reviewed", "blocked"):
                errors.append("当前状态不允许 review，请先完成实现和自查。")
        elif run_command == "verify":
            if phase not in ("reviewed", "done", "blocked"):
                errors.append("当前状态不允许 verify，请先完成 review。")
        if session_meta and run_command in ("start-implement", "finish-implement", "review", "verify"):
            standard = validate_standard_session(config, require_notification=False)
            for item in standard.get("errors", []):
                if "notification.json" not in str(item):
                    errors.append(str(item))
        return {
            "valid": not errors,
            "phase": phase,
            "allowed_next": allowed_next,
            "errors": errors,
            "status_phase": status.get("phase", "") if isinstance(status, dict) else "",
        }
    sl_command = RUN_COMMAND_TO_SL_COMMAND.get(run_command, "")
    if not sl_command:
        return {"valid": True, "phase": "", "allowed_next": []}
    if run_command in ("status", "recover"):
        state = recover_workflow_state_from_artifacts(config)
        return {"valid": True, "phase": state.get("phase", ""), "allowed_next": state.get("allowed_next", [])}
    if sl_command == "/sl:propose":
        return {"valid": True, "phase": read_sl_state(config).get("phase", "draft"), "allowed_next": ["/sl:propose"]}

    state = recover_sl_state_from_artifacts(config)
    phase = str(state.get("phase", "") or "draft").strip()
    allowed_next = [str(item) for item in state.get("allowed_next", [])]
    artifacts = state.get("artifacts", {})
    if not isinstance(artifacts, dict):
        artifacts = {}

    errors: list[str] = []
    if sl_command == "/sl:bridge":
        if not openspec_change_name(config):
            errors.append(
                "缺少当前 OpenSpec change。请先执行 /sl:propose <change-name>；"
                "workspace.yml 支持相对路径、${demand_name} 和 openspec.changes_dir，不要改成绝对路径或手工配置 openspec.change_dir。"
            )
        if phase not in ("proposed", "bridged"):
            errors.append("当前状态不允许执行 /sl:bridge，请先执行 /sl:propose <change-name>，或在进入交付前停留在 bridged 阶段重新桥接。")
        for key in ("proposal", "design", "tasks"):
            if not _sl_state_artifact_exists(str(artifacts.get(key, ""))):
                errors.append(f"缺少 OpenSpec 产物：{key}")
    elif sl_command == "/sl:plan":
        if phase not in ("bridged", "planned", "blocked") and sl_command not in allowed_next:
            errors.append("当前状态不允许重新计划，请先完成 /sl:bridge，或继续当前活跃交付会话。")
        if phase in ("implementing", "self_checked", "reviewed", "verified"):
            errors.append("当前已有活跃 session 正在交付中，不能重新执行 /sl:plan。请继续当前 /sl:apply、/sl:review 或 /sl:verify。")
        if phase == "proposed":
            errors.append("/sl:propose 后不能直接进入交付，必须先执行 /sl:bridge。")
        if not _sl_state_artifact_exists(str(artifacts.get("todo", ""))):
            errors.append("缺少桥接 todo.md，请先执行 /sl:bridge。")
        bridged_hash = str(artifacts.get("tasks_sha256", "")).strip()
        current_hash = openspec_tasks_hash(config)
        if bridged_hash and current_hash and bridged_hash != current_hash:
            errors.append("OpenSpec tasks.md 已变化，请重新执行 /sl:bridge 生成待审核 todo.md。")
    elif sl_command == "/sl:apply":
        if phase not in ("bridged", "planned", "implementing", "reviewed", "blocked") and sl_command not in allowed_next:
            errors.append("当前状态不允许进入交付，请先完成 /sl:bridge 并人工审核 todo.md。")
        if phase == "proposed":
            errors.append("/sl:propose 后不能直接进入交付，必须先执行 /sl:bridge。")
        if not _sl_state_artifact_exists(str(artifacts.get("todo", ""))):
            errors.append("缺少桥接 todo.md，请先执行 /sl:bridge。")
        bridged_hash = str(artifacts.get("tasks_sha256", "")).strip()
        current_hash = openspec_tasks_hash(config)
        if bridged_hash and current_hash and bridged_hash != current_hash:
            errors.append("OpenSpec tasks.md 已变化，请重新执行 /sl:bridge 并审核新的 todo.md。")
    elif sl_command == "/sl:review":
        if phase not in ("self_checked", "reviewed", "blocked"):
            errors.append("当前状态不允许 review，请先完成实现和自查。")
    elif sl_command == "/sl:verify":
        if phase not in ("reviewed", "verified", "blocked"):
            errors.append("当前状态不允许 verify，请先完成 review。")
    elif sl_command == "/sl:archive-check":
        if phase != "verified":
            errors.append("当前状态不允许 archive-check，请先完成 /sl:verify。")
    elif sl_command == "/sl:archive":
        if phase != "archive_ready":
            errors.append("当前状态不允许 archive，请先完成 /sl:archive-check 且结果为 safe_merge。")

    return {
        "valid": not errors,
        "phase": phase,
        "allowed_next": allowed_next,
        "errors": errors,
        "state_path": str(sl_state_path(config)),
    }


