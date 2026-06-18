from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .io_utils import read_json, write_json
from .time_utils import now_iso


def workflow_source(config: dict[str, Any]) -> str:
    return str(config.get("workflow_source", "todo")).strip() or "todo"


def _demand_runtime_dir(root: Path | str, demand_name: str) -> Path:
    return Path(str(root)).resolve() / ".speclane" / "demands" / str(demand_name).strip()


def artifacts_dir(config: dict[str, Any]) -> Path:
    root = Path(str(config["__workspace_root"])).resolve()
    demand_name = str(config.get("__demand_name", "")).strip()
    if demand_name:
        return _demand_runtime_dir(root, demand_name)
    return root / ".speclane"


def active_openspec_change_path(root: Path | str, demand_name: str = "") -> Path:
    selected = str(demand_name).strip()
    if selected:
        return _demand_runtime_dir(root, selected) / "current-openspec-change.json"
    return Path(str(root)).resolve() / ".speclane" / "current-openspec-change.json"


def sl_state_path(config: dict[str, Any]) -> Path:
    return artifacts_dir(config) / "sl-state.json"


def todo_state_path(config: dict[str, Any]) -> Path:
    return artifacts_dir(config) / "todo-state.json"


def workflow_state_path(config: dict[str, Any]) -> Path:
    return todo_state_path(config) if workflow_source(config) == "todo" else sl_state_path(config)


def sessions_dir(config: dict[str, Any]) -> Path:
    return artifacts_dir(config) / "sessions"


def current_session_file(config: dict[str, Any]) -> Path:
    return artifacts_dir(config) / "current-session.json"


def output_dir(config: dict[str, Any]) -> Path:
    return Path(str(config["output_dir"])).resolve()


def qa_dir(config: dict[str, Any]) -> Path:
    root = Path(str(config["__workspace_root"])).resolve()
    demand_name = str(config.get("__demand_name", "")).strip()
    if demand_name:
        return root / "demands" / demand_name / "qa"
    return root / "qa"


def session_data_dir(config: dict[str, Any], session_id: str) -> Path:
    return sessions_dir(config) / session_id


def session_report_dir(config: dict[str, Any], session_id: str) -> Path:
    return output_dir(config) / session_id


def _normalize_session_meta(config: dict[str, Any], session_meta: dict[str, Any]) -> dict[str, Any]:
    session_id = str(session_meta.get("session_id", "")).strip()
    if not session_id:
        raise FileNotFoundError("尚未发现当前会话，请先执行 plan 创建新的工作流会话。")
    started_at = (
        str(session_meta.get("started_at", "")).strip()
        or str(session_meta.get("created_at", "")).strip()
        or now_iso()
    )
    return {
        "session_id": session_id,
        "created_at": str(session_meta.get("created_at", "")).strip() or now_iso(),
        "started_at": started_at,
        "workspace": str(Path(str(config["__workspace_root"])).resolve()),
        "data_dir": str(session_data_dir(config, session_id)),
        "report_dir": str(session_report_dir(config, session_id)),
    }


def create_session(config: dict[str, Any]) -> dict[str, Any]:
    ensure_runtime_dirs(config)
    session_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    started_at = now_iso()
    session_meta = _normalize_session_meta(
        config,
        {
            "session_id": session_id,
            "created_at": started_at,
            "started_at": started_at,
        },
    )
    Path(session_meta["data_dir"]).mkdir(parents=True, exist_ok=True)
    write_json(current_session_file(config), session_meta)
    return session_meta


def current_session_meta(config: dict[str, Any]) -> dict[str, Any]:
    session_meta = read_json(current_session_file(config), {})
    return _normalize_session_meta(config, session_meta)


def current_session_status(config: dict[str, Any], session_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = session_meta or current_session_meta(config)
    status = read_json(data_artifact_path(config, "status.json", meta), {})
    return status if isinstance(status, dict) else {}


def current_session_is_stale(config: dict[str, Any]) -> bool:
    raw = read_json(current_session_file(config), {})
    if not isinstance(raw, dict) or not raw.get("session_id"):
        return False
    session_id = str(raw.get("session_id", "")).strip()
    expected_report_dir = session_report_dir(config, session_id).resolve()
    raw_report_dir = str(raw.get("report_dir", "")).strip()
    if raw_report_dir and Path(raw_report_dir).expanduser().resolve() != expected_report_dir:
        return True
    raw_workspace = str(raw.get("workspace", "")).strip()
    if raw_workspace and Path(raw_workspace).expanduser().resolve() != Path(str(config["__workspace_root"])).resolve():
        return True
    return False



def active_session_for_plan(config: dict[str, Any]) -> dict[str, Any] | None:
    if current_session_is_stale(config):
        return None
    try:
        session_meta = current_session_meta(config)
    except FileNotFoundError:
        return None
    data_dir = Path(str(session_meta["data_dir"]))
    has_plan = data_artifact_path(config, "plan.json", session_meta).exists()
    status = current_session_status(config, session_meta)
    status_phase = str(status.get("phase", "") or "").strip()
    if status_phase in ("done", "archived", "blocked"):
        return None
    if not has_plan and data_dir.exists():
        return {
            "session": session_meta,
            "status": status,
            "phase": status_phase or "draft",
            "incomplete": True,
        }
    if not has_plan:
        return None
    return {
        "session": session_meta,
        "status": status,
        "phase": status_phase or "plan",
        "incomplete": False,
    }


def ensure_plan_can_run(config: dict[str, Any]) -> dict[str, Any] | None:
    active = active_session_for_plan(config)
    if not active:
        return None
    phase = str(active.get("phase", "")).strip()
    session = active.get("session", {})
    if active.get("incomplete"):
        return active
    if phase in ("plan", "wait_confirm_plan"):
        return active
    raise RuntimeError(
        f"当前已有活跃 session 正在交付中，禁止重新执行 /sl:plan："
        f"session_id={session.get('session_id', '')}, phase={phase}。"
        "请继续当前 /sl:apply 链路，不要创建新的 plan/session。"
    )


def data_artifact_path(config: dict[str, Any], name: str, session_meta: dict[str, Any] | None = None) -> Path:
    meta = _normalize_session_meta(config, session_meta or current_session_meta(config))
    return Path(meta["data_dir"]) / name


def report_artifact_path(config: dict[str, Any], name: str, session_meta: dict[str, Any] | None = None) -> Path:
    meta = _normalize_session_meta(config, session_meta or current_session_meta(config))
    return Path(meta["report_dir"]) / name


def workspace_relative_path(config: dict[str, Any], path: Path | str) -> str:
    resolved = Path(str(path)).resolve()
    root = Path(str(config.get("__workspace_root", ""))).resolve()
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        return resolved.name


def artifact_path(config: dict[str, Any], name: str) -> Path:
    return data_artifact_path(config, name)


def ensure_runtime_dirs(config: dict[str, Any]) -> None:
    artifacts_dir(config).mkdir(parents=True, exist_ok=True)
    sessions_dir(config).mkdir(parents=True, exist_ok=True)
    output_dir(config).mkdir(parents=True, exist_ok=True)

def planned_codebases(config: dict[str, Any], session_meta: dict[str, Any] | None = None) -> list[Path]:
    meta = session_meta or current_session_meta(config)
    plan = read_json(data_artifact_path(config, "plan.json", meta), {})
    paths = plan.get("resolved_code_paths", [])
    if isinstance(paths, list) and paths:
        return [Path(str(item)).resolve() for item in paths if str(item).strip()]
    path = str(plan.get("resolved_code_path", "")).strip()
    if path:
        return [Path(path).resolve()]
    return [Path(str(config["code_path"])).resolve()]


def planned_codebase(config: dict[str, Any], session_meta: dict[str, Any] | None = None) -> Path:
    return planned_codebases(config, session_meta)[0]
