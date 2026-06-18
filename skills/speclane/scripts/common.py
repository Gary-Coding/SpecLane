#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    looks_like_project_root,
    resolve_target_codebase,
    resolve_target_codebases,
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


def workflow_lock_path(config: dict[str, Any]) -> Path:
    return artifacts_dir(config) / "workflow.lock"


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_workflow_lock(config: dict[str, Any], command: str, stale_seconds: int = 1800) -> Path:
    ensure_runtime_dirs(config)
    path = workflow_lock_path(config)
    payload = {
        "pid": os.getpid(),
        "command": command,
        "created_at": now_iso(),
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags)
    except FileExistsError:
        existing = read_json(path, {})
        pid = int(existing.get("pid", 0) or 0) if isinstance(existing, dict) else 0
        created_at = parse_iso_datetime(str(existing.get("created_at", ""))) if isinstance(existing, dict) else None
        age = (datetime.now(timezone.utc) - created_at).total_seconds() if created_at else stale_seconds + 1
        if age > stale_seconds or not process_is_running(pid):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return acquire_workflow_lock(config, command, stale_seconds=stale_seconds)
        raise RuntimeError(
            f"检测到工作流正在执行：pid={pid}, command={existing.get('command', '') if isinstance(existing, dict) else ''}。"
            "请等待当前命令结束后重试。"
        )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return path


def release_workflow_lock(path: Path | None) -> None:
    if not path:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _stringify_workspace_vars(raw_vars: Any) -> dict[str, str]:
    if raw_vars in ("", None):
        return {}
    if not isinstance(raw_vars, dict):
        raise ValueError("workspace.yml 中的 vars 必须是对象。")
    variables: dict[str, str] = {}
    for key, value in raw_vars.items():
        key_text = str(key).strip()
        if not key_text:
            raise ValueError("workspace.yml 中的 vars 不能包含空 key。")
        if isinstance(value, (dict, list)):
            raise ValueError(f"workspace.yml 中的 vars.{key_text} 必须是标量值。")
        variables[key_text] = str(value)
    return variables


def _expand_workspace_value(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name.startswith("vars."):
                name = name[5:]
            if name not in variables:
                raise ValueError(f"workspace.yml 中引用了未定义变量：{match.group(1)}")
            return variables[name]

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}", replace, value)
    if isinstance(value, list):
        return [_expand_workspace_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _expand_workspace_value(item, variables) for key, item in value.items()}
    return value


def expand_workspace_variables(config: dict[str, Any], root: Path, extra_vars: dict[str, str] | None = None) -> dict[str, Any]:
    user_vars = _stringify_workspace_vars(config.get("vars", {}))
    if extra_vars:
        for key, value in extra_vars.items():
            if value not in ("", None):
                user_vars[str(key)] = str(value)
    variables = {
        "workspace_root": str(root),
        **user_vars,
    }
    expanded = _expand_workspace_value(config, variables)
    if not isinstance(expanded, dict):
        raise ValueError("工作空间配置必须是对象结构。")
    expanded["vars"] = user_vars
    return expanded


def resolve_workspace_path(root: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


def is_url(value: Any) -> bool:
    parsed = urlparse(str(value).strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def is_lark_doc_url(value: Any) -> bool:
    text = str(value).strip()
    if not is_url(text):
        return False
    parsed = urlparse(text)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if not any(marker in host for marker in ("feishu.cn", "larksuite.com", "larksuite.cn")):
        return False
    return any(marker in path for marker in ("/doc", "/docx", "/wiki"))


def normalize_verify_commands(raw: Any) -> dict[str, str]:
    if raw in ("", None):
        return {}
    if isinstance(raw, str):
        command = raw.strip()
        return {"default": command} if command else {}
    if not isinstance(raw, dict):
        raise ValueError("workspace.yml 中的 verify_commands 必须是字符串或对象。")
    commands: dict[str, str] = {}
    for key, value in raw.items():
        key_text = str(key).strip()
        value_text = str(value).strip()
        if not key_text:
            raise ValueError("workspace.yml 中的 verify_commands 不能包含空 key。")
        if isinstance(value, (dict, list)):
            raise ValueError(f"workspace.yml 中的 verify_commands.{key_text} 必须是字符串。")
        if value_text:
            commands[key_text] = value_text
    return commands


def workspace_root(workspace: Path | None = None) -> Path:
    return (workspace or Path.cwd()).expanduser().resolve()


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def user_skill_config_dir() -> Path:
    return Path.home() / ".speclane"


def workspace_config_path(workspace: Path | None = None) -> Path:
    root = workspace_root(workspace)
    workspace_yml = root / "workspace.yml"
    if workspace_yml.exists():
        return workspace_yml
    legacy_config = root / "config.yml"
    if legacy_config.exists():
        return legacy_config
    return workspace_yml


def demand_registry_dir(root: Path | str) -> Path:
    return Path(str(root)).resolve() / ".speclane" / "demands"


def demand_runtime_dir(root: Path | str, demand_name: str) -> Path:
    return demand_registry_dir(root) / validate_demand_name(demand_name)


def active_demand_path(root: Path | str) -> Path:
    return Path(str(root)).resolve() / ".speclane" / "active-demand.yml"


def validate_demand_name(demand_name: str) -> str:
    normalized = str(demand_name).strip()
    if not normalized:
        raise ValueError("需求名称不能为空。")
    if "/" in normalized or "\\" in normalized:
        raise ValueError("需求名称不能包含路径分隔符。")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", normalized):
        raise ValueError("需求名称必须匹配 [A-Za-z0-9][A-Za-z0-9._-]*。")
    return normalized


def read_active_demand(root: Path | str) -> str:
    data = parse_simple_yaml(active_demand_path(root).read_text(encoding="utf-8")) if active_demand_path(root).exists() else {}
    if not isinstance(data, dict):
        return ""
    return str(data.get("demand_name", "")).strip()


def write_active_demand(root: Path | str, demand_name: str) -> Path:
    selected = validate_demand_name(demand_name)
    path = active_demand_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"demand_name: {selected}\nupdated_at: {now_iso()}\n", encoding="utf-8")
    return path


def configured_demand_name(config: dict[str, Any]) -> str:
    vars_map = config.get("vars", {})
    if isinstance(vars_map, dict):
        return str(vars_map.get("demand_name", "")).strip()
    return ""


def workspace_demands(raw_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    demands = raw_config.get("demands", {})
    if demands in ("", None):
        return {}
    if not isinstance(demands, list):
        raise ValueError("workspace.yml 中的 demands 必须是数组，每项包含 name 和 desc。")
    normalized: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(demands, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"workspace.yml 中的 demands[{index}] 必须是对象。")
        name = validate_demand_name(str(item.get("name", "")).strip())
        if name in normalized:
            raise ValueError(f"workspace.yml 中 demands 存在重复需求名称：{name}")
        normalized[name] = dict(item)
    return normalized


def resolve_requested_demand(root: Path, raw_config: dict[str, Any], explicit_demand: str | None = None) -> str:
    demands = workspace_demands(raw_config)
    requested = str(explicit_demand or "").strip() or str(os.environ.get("SPECLANE_DEMAND_NAME", "")).strip()
    if requested:
        selected = validate_demand_name(requested)
        if demands and selected not in demands:
            raise ValueError(f"workspace.yml 中不存在需求配置：demands[].name={selected}")
        return selected
    active = read_active_demand(root)
    if active:
        selected = validate_demand_name(active)
        if demands and selected not in demands:
            raise ValueError(f"当前 active demand 不存在于 workspace.yml.demands：{selected}")
        return selected
    vars_map = raw_config.get("vars", {})
    if isinstance(vars_map, dict):
        configured = str(vars_map.get("demand_name", "")).strip()
        if configured:
            selected = validate_demand_name(configured)
            if not demands or selected in demands:
                return selected
    if len(demands) == 1:
        return next(iter(demands))
    return ""


def apply_demand_defaults(config: dict[str, Any], demand_name: str) -> dict[str, Any]:
    if not demand_name:
        return config
    defaults = config.get("demand_defaults", {})
    if defaults in ("", None):
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError("workspace.yml 中的 demand_defaults 必须是对象。")
    for key in ("workflow_source", "mode", "demand_file", "todo_file", "output_dir", "reference_files"):
        if key in defaults and config.get(key) in ("", None, [], {}):
            config[key] = defaults[key]
    config.setdefault("demand_file", f"demands/{demand_name}/input/需求.md")
    config.setdefault("todo_file", f"demands/{demand_name}/spec/bridge/todo.md")
    config.setdefault("output_dir", f"demands/{demand_name}/rd/output")
    return config


def load_workspace_demand_config(raw_config: dict[str, Any], demand_name: str) -> dict[str, Any]:
    if not demand_name:
        return {}
    demands = workspace_demands(raw_config)
    if not demands:
        return {}
    loaded = demands.get(demand_name, {})
    if loaded in ("", None):
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError(f"workspace.yml 中的 demands.{demand_name} 必须是对象。")
    return loaded


def skill_config_path() -> Path:
    return user_skill_config_dir() / "skill-config.yml"


def skill_config_example_path() -> Path:
    return skill_root() / "config.example.yml"


def default_skill_config() -> dict[str, Any]:
    return {
        "version": 1,
        "notification": {
            "pushplus": {
                "token": "",
                "ordinary": {
                    "enabled": False,
                    "channel": "wechat",
                    "template": "markdown",
                },
            },
            "feishu": {
                "enabled": False,
                "webhook_url": "",
                "secret": "",
            },
        },
    }


def default_skill_config_text() -> str:
    return """version: 1
notification:
  pushplus:
    token: ""
    ordinary:
      enabled: false
      channel: wechat
      template: markdown
  feishu:
    enabled: false
    webhook_url: ""
    secret: ""
"""


def ensure_user_skill_config() -> tuple[Path, bool]:
    config_path = skill_config_path()
    if config_path.exists():
        return config_path, False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(default_skill_config_text(), encoding="utf-8")
    return config_path, True


def load_skill_config() -> dict[str, Any]:
    config_path, created = ensure_user_skill_config()
    config = default_skill_config()
    if config_path.exists():
        loaded = parse_simple_yaml(config_path.read_text(encoding="utf-8"))
        if loaded:
            config.update(loaded)

    if config.get("version") != 1:
        raise ValueError("~/.speclane/skill-config.yml 中的 version 必须为 1。")

    notification = config.get("notification", {})
    if notification in ("", None):
        notification = {}
    if not isinstance(notification, dict):
        raise ValueError("~/.speclane/skill-config.yml 中的 notification 必须是对象。")

    pushplus = notification.get("pushplus", {})
    if pushplus in ("", None):
        pushplus = {}
    if not isinstance(pushplus, dict):
        raise ValueError("~/.speclane/skill-config.yml 中的 notification.pushplus 必须是对象。")

    pushplus_token = str(pushplus.get("token", "")).strip()
    ordinary_raw = pushplus.get("ordinary", {})
    feishu_raw = notification.get("feishu", {})

    if ordinary_raw in ("", None):
        ordinary_raw = {}
    if feishu_raw in ("", None):
        feishu_raw = {}
    if not isinstance(ordinary_raw, dict):
        raise ValueError("~/.speclane/skill-config.yml 中的 notification.pushplus.ordinary 必须是对象。")
    if not isinstance(feishu_raw, dict):
        raise ValueError("~/.speclane/skill-config.yml 中的 notification.feishu 必须是对象。")

    ordinary_channel = str(ordinary_raw.get("channel", "wechat")).strip() or "wechat"
    ordinary_template = str(ordinary_raw.get("template", "markdown")).strip() or "markdown"
    ordinary_enabled = bool(ordinary_raw.get("enabled", False))

    feishu_enabled = bool(feishu_raw.get("enabled", False))
    feishu_webhook_url = str(feishu_raw.get("webhook_url", "")).strip()
    feishu_secret = str(feishu_raw.get("secret", "")).strip()

    if not ordinary_channel:
        raise ValueError("~/.speclane/skill-config.yml 中的 notification.pushplus.ordinary.channel 不能为空。")
    if ordinary_enabled and not pushplus_token:
        raise ValueError("启用 PushPlus 通知时，~/.speclane/skill-config.yml 中的 notification.pushplus.token 不能为空。")
    if feishu_enabled and not feishu_webhook_url:
        raise ValueError("启用飞书通知时，~/.speclane/skill-config.yml 中的 notification.feishu.webhook_url 不能为空。")
    if feishu_webhook_url and not feishu_webhook_url.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/"):
        raise ValueError("notification.feishu.webhook_url 必须是飞书自定义机器人的 webhook 地址。")

    config["notification"] = {
        "pushplus": {
            "token": pushplus_token,
            "ordinary": {
                "enabled": ordinary_enabled,
                "channel": ordinary_channel,
                "template": ordinary_template,
            },
        },
        "feishu": {
            "enabled": feishu_enabled,
            "webhook_url": feishu_webhook_url,
            "secret": feishu_secret,
        },
    }
    config["__skill_root"] = str(skill_root())
    config["__skill_config_example_path"] = str(skill_config_example_path())
    config["__skill_config_path"] = str(config_path)
    config["__skill_config_created"] = created
    return config


def load_workspace_config(workspace: Path | None = None) -> dict[str, Any]:
    root = workspace_root(workspace)
    config_path = workspace_config_path(root)
    if not config_path.exists():
        raise FileNotFoundError(f"未找到工作空间配置文件：{config_path}")

    raw_config = parse_simple_yaml(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw_config, dict):
        raise ValueError("workspace.yml 顶层必须是对象。")
    demand_name = resolve_requested_demand(root, raw_config)
    demand_config = load_workspace_demand_config(raw_config, demand_name)
    merged_raw = dict(raw_config)
    if demand_config:
        for key, value in demand_config.items():
            if key in ("version", "name", "desc"):
                continue
            merged_raw[key] = value
    if demand_name:
        merged_raw.pop("demands", None)
        vars_map = dict(merged_raw.get("vars", {}) if isinstance(merged_raw.get("vars", {}), dict) else {})
        vars_map["demand_name"] = demand_name
        merged_raw["vars"] = vars_map
        merged_raw = apply_demand_defaults(merged_raw, demand_name)
    config = expand_workspace_variables(merged_raw, root, {"demand_name": demand_name} if demand_name else None)
    config.setdefault("version", 1)
    config.setdefault("mode", "manual")
    config.setdefault("workflow_source", "todo")
    config.setdefault("reference_files", [])

    if config.get("version") != 1:
        raise ValueError("workspace.yml 中的 version 必须为 1。")
    if config.get("mode") not in ("manual", "auto"):
        raise ValueError("workspace.yml 中的 mode 只支持 manual 或 auto。")
    if config.get("workflow_source") not in ("todo", "openspec"):
        raise ValueError("workspace.yml 中的 workflow_source 只支持 todo 或 openspec。")

    todo_file = resolve_workspace_path(root, config.get("todo_file", ""))
    demand_file_raw = config.get("demand_file", "")
    demand_source = str(demand_file_raw).strip() if demand_file_raw not in ("", None) else ""
    demand_file = None
    demand_source_type = ""
    if demand_source:
        if is_lark_doc_url(demand_source):
            demand_source_type = "lark_doc"
        elif is_url(demand_source):
            raise ValueError("workspace.yml 中的 demand_file URL 目前只支持飞书/Lark 云文档链接。")
        else:
            demand_file = resolve_workspace_path(root, demand_source)
            demand_source_type = "local"

    code_path = resolve_workspace_path(root, config.get("code_path", ""))
    if not code_path.exists():
        raise ValueError(f"workspace.yml 中的 code_path 不存在：{code_path}")

    output_dir = resolve_workspace_path(root, config.get("output_dir", ""))

    reference_files = config.get("reference_files", [])
    if reference_files == {}:
        reference_files = []
    if not isinstance(reference_files, list):
        raise ValueError("workspace.yml 中的 reference_files 必须是数组。")
    normalized_refs: list[str] = []
    for item in reference_files:
        path = resolve_workspace_path(root, item)
        normalized_refs.append(str(path))

    config["todo_file"] = str(todo_file.resolve())
    config["demand_file"] = demand_source if demand_source_type == "lark_doc" else (str(demand_file.resolve()) if demand_file else "")
    config["demand_source_type"] = demand_source_type
    config["code_path"] = str(code_path.resolve())
    config["output_dir"] = str(output_dir.resolve())
    config["reference_files"] = normalized_refs
    config["workflow_source"] = str(config.get("workflow_source", "todo")).strip() or "todo"
    config["verify_commands"] = normalize_verify_commands(config.get("verify_commands", {}))

    openspec_raw = config.get("openspec", {})
    if openspec_raw in ("", None):
        openspec_raw = {}
    if not isinstance(openspec_raw, dict):
        raise ValueError("workspace.yml 中的 openspec 必须是对象。")
    openspec: dict[str, Any] = {}
    if config["workflow_source"] == "openspec":
        changes_dir_raw = openspec_raw.get("changes_dir", "")
        change_dir_raw = openspec_raw.get("change_dir", "")
        changes_dir = resolve_workspace_path(root, changes_dir_raw) if changes_dir_raw not in ("", None) else None
        configured_change_dir = resolve_workspace_path(root, change_dir_raw) if change_dir_raw not in ("", None) else None

        active_change = read_json(active_openspec_change_path(root, demand_name), {})
        state_root = demand_runtime_dir(root, demand_name) if demand_name else root / ".speclane"
        bridge_context = read_json(state_root / "openspec-bridge-context.json", {})
        st_state = read_json(state_root / "sl-state.json", {})
        active_change_name = str(active_change.get("change_name", "")).strip()
        bridge_change_name = str(bridge_context.get("change_name", "")).strip()
        state_change_name = str(st_state.get("current_change", "")).strip()
        configured_change_name = str(openspec_raw.get("change_name", "")).strip()

        if changes_dir is None:
            if configured_change_dir is not None:
                changes_dir = configured_change_dir if configured_change_dir.name == "changes" else configured_change_dir.parent
            else:
                if demand_name:
                    changes_dir = (root / "demands" / demand_name / "spec" / "openspec" / "changes").resolve()
                else:
                    changes_dir = (root / "openspec" / "changes").resolve()

        def usable_change_name(candidate: str, require_existing_dir: bool) -> str:
            normalized = str(candidate).strip()
            if not normalized or not re.fullmatch(r"[a-z][a-z0-9-]*", normalized):
                return ""
            if require_existing_dir and not (changes_dir / normalized).exists():
                return ""
            return normalized

        inferred_change_name = (
            usable_change_name(active_change_name, True)
            or usable_change_name(configured_change_name, False)
            or usable_change_name(state_change_name, True)
            or usable_change_name(bridge_change_name, True)
        )

        if inferred_change_name:
            change_name = inferred_change_name
            change_dir = changes_dir / change_name
        elif configured_change_dir is not None and configured_change_dir.name != "changes":
            change_dir = configured_change_dir
            change_name = configured_change_name or change_dir.name
        else:
            change_name = ""
            change_dir = changes_dir / change_name if change_name else changes_dir

        tasks_file = resolve_workspace_path(root, openspec_raw.get("tasks_file", change_dir / "tasks.md"))
        proposal_file = resolve_workspace_path(root, openspec_raw.get("proposal_file", change_dir / "proposal.md"))
        design_file = resolve_workspace_path(root, openspec_raw.get("design_file", change_dir / "design.md"))
        specs_dir = resolve_workspace_path(root, openspec_raw.get("specs_dir", change_dir / "specs"))
        writeback_dir = resolve_workspace_path(root, openspec_raw.get("writeback_dir", change_dir / "speclane"))
        openspec = {
            "changes_dir": str(changes_dir.resolve()),
            "change_dir": str(change_dir.resolve()),
            "tasks_file": str(tasks_file.resolve()),
            "proposal_file": str(proposal_file.resolve()),
            "design_file": str(design_file.resolve()),
            "specs_dir": str(specs_dir.resolve()),
            "writeback_dir": str(writeback_dir.resolve()),
            "change_name": change_name,
        }
    config["openspec"] = openspec

    skill_config = load_skill_config()
    config["notification"] = skill_config.get("notification", {})
    config["__workspace_root"] = str(root)
    config["__demand_name"] = demand_name
    config["__demand_config_source"] = f"{config_path}#demands.{demand_name}" if demand_name else ""
    config["__config_path"] = str(config_path)
    config["__skill_root"] = str(skill_root())
    config["__skill_config_path"] = str(skill_config_path())
    config["__skill_config_created"] = bool(skill_config.get("__skill_config_created", False))
    config["__skill_config_example_path"] = str(skill_config_example_path())
    return config


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


def assert_managed_artifact(path: Path, config: dict[str, Any]) -> Path:
    resolved = path.resolve()
    data_root = artifacts_dir(config).resolve()
    output_root = output_dir(config).resolve()
    qa_root = qa_dir(config).resolve()
    writeback = openspec_writeback_dir(config).resolve() if workflow_source(config) == "openspec" else None
    allowed_roots = [data_root, output_root, qa_root]
    if writeback:
        allowed_roots.append(writeback)
        allowed_roots.append(openspec_archive_root(config).resolve())
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise ValueError(f"拒绝写入非工作流托管产物：{resolved}")
    return resolved


def write_managed_json(config: dict[str, Any], path: Path, payload: Any) -> None:
    write_json(assert_managed_artifact(path, config), payload)


def write_managed_text(config: dict[str, Any], path: Path, content: str) -> None:
    write_text(assert_managed_artifact(path, config), content)


def todo_template() -> str:
    return """# 限制条件
- 修改的服务是 your-service-name

# 待办事项

## 模块一：在这里写大需求模块名称
- [ ] 在这里写主任务 1
1. 在这里写子要求 1
2. 在这里写子要求 2

- [ ] 在这里写主任务 2
1. 在这里写子要求 1
2. 在这里写子要求 2

## 模块二：在这里写第二个大需求模块名称
- [ ] 在这里写主任务 3
- [ ] 在这里写主任务 4

## 验收补充
- [ ] 在这里写需要补充的测试、文档或验证要求
"""


def demand_path(config: dict[str, Any]) -> Path | None:
    path_text = str(config.get("demand_file", "")).strip()
    if not path_text or str(config.get("demand_source_type", "")).strip() == "lark_doc" or is_url(path_text):
        return None
    return Path(path_text).resolve()


def lark_cli_available() -> bool:
    return bool(shutil.which("lark-cli"))


def lark_cli_install_message() -> str:
    return "\n".join(
        [
            "检测到 demand_file 是飞书/Lark 云文档，但本机未安装官方 lark-cli。",
            "请先安装并完成授权：",
            "1. npx @larksuite/cli@latest install",
            "2. lark-cli config init --new",
            "3. lark-cli auth login --recommend",
            "4. lark-cli auth status",
            "安装和授权完成后重新执行 /sl:propose <change-name>。",
        ]
    )


def _extract_lark_cli_text(stdout: str) -> str:
    text = stdout.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    def pick(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("content", "markdown", "text", "body", "document", "doc"):
                picked = pick(value.get(key))
                if picked:
                    return picked
            for item in value.values():
                picked = pick(item)
                if picked:
                    return picked
        if isinstance(value, list):
            parts = [pick(item) for item in value]
            return "\n\n".join(item for item in parts if item)
        return ""

    return pick(parsed) or text


def read_lark_doc_demand(config: dict[str, Any], demand_url: str) -> dict[str, Any]:
    if not lark_cli_available():
        raise RuntimeError(lark_cli_install_message())
    commands = [
        ["lark-cli", "docs", "+fetch", "--api-version", "v2", "--doc", demand_url, "--doc-format", "markdown", "--detail", "full"],
        ["lark-cli", "docs", "+fetch", "--api-version", "v2", "--doc", demand_url, "--doc-format", "markdown"],
        ["lark-cli", "docs", "+fetch", "--doc", demand_url, "--doc-format", "markdown"],
        ["lark-cli", "docs", "+fetch", "--url", demand_url, "--doc-format", "markdown"],
    ]
    attempts: list[dict[str, Any]] = []
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=str(config.get("__workspace_root", os.getcwd())),
                text=True,
                capture_output=True,
                check=False,
                timeout=90,
            )
        except FileNotFoundError:
            raise RuntimeError(lark_cli_install_message())
        except subprocess.TimeoutExpired:
            attempts.append(
                {
                    "command": command,
                    "returncode": "timeout",
                    "stdout": "",
                    "stderr": "lark-cli docs +fetch timeout",
                }
            )
            continue
        attempts.append(
            {
                "command": command,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if result.returncode == 0:
            content = _extract_lark_cli_text(result.stdout)
            if content:
                return {
                    "source_type": "lark_doc",
                    "source": demand_url,
                    "content": content,
                    "command": command,
                    "attempts": attempts,
                }
    last = attempts[-1] if attempts else {}
    raise RuntimeError(
        "读取飞书/Lark 云文档失败。请确认 lark-cli 已完成授权且当前账号有文档访问权限。\n"
        "建议执行：lark-cli auth status；如未登录，执行：lark-cli auth login --recommend。\n"
        f"最后一次错误：{str(last.get('stderr') or last.get('stdout') or '').strip()}"
    )


def read_demand_source(config: dict[str, Any]) -> dict[str, Any]:
    source = str(config.get("demand_file", "")).strip()
    if not source:
        return {"source_type": "", "source": "", "content": "", "command": [], "attempts": []}
    if str(config.get("demand_source_type", "")).strip() == "lark_doc" or is_lark_doc_url(source):
        return read_lark_doc_demand(config, source)
    path = Path(source)
    return {
        "source_type": "local",
        "source": str(path.resolve()),
        "content": read_text(path),
        "command": [],
        "attempts": [],
    }


def ensure_workflow_inputs(config: dict[str, Any], *, allow_bridge_write: bool = False) -> dict[str, Any]:
    source = workflow_source(config)
    todo_file = todo_path(config)
    result = {
        "workflow_source": source,
        "todo_path": str(todo_file),
        "todo_created": False,
        "todo_needs_edit": False,
        "bridge_generated": False,
        "bridge_source": "",
    }
    if source == "todo":
        if not todo_file.exists():
            write_text(todo_file, todo_template())
            result["todo_created"] = True
        todo_text = read_text(todo_file)
        result["todo_needs_edit"] = is_todo_template_placeholder(todo_text)
        return result

    change_name = openspec_change_name(config)
    if not change_name:
        raise ValueError(
            "缺少当前 OpenSpec change。请先执行 /sl:propose <change-name> 记录 active change；"
            "不需要把 workspace.yml 改成绝对路径，也不需要显式配置 openspec.change_dir。"
        )
    tasks_file = openspec_tasks_path(config)
    if not tasks_file.exists():
        raise FileNotFoundError(
            f"OpenSpec tasks 文件不存在：{tasks_file}。请确认已执行 /sl:propose {change_name} 并生成 tasks.md；"
            "不要手工生成桥接产物。"
        )
    tasks_text = read_text(tasks_file)
    existing = read_text(todo_file)
    if not allow_bridge_write:
        if not existing.strip():
            raise FileNotFoundError(
                f"桥接 todo 文件不存在：{todo_file}。OpenSpec 模式下只有显式执行 /sl:bridge "
                "才允许从 tasks.md 生成 todo.md；/sl:init、/sl:propose、/sl:plan、/sl:apply 都不能自动生成桥接 todo。"
            )
        result["bridge_source"] = str(tasks_file)
        result["todo_needs_edit"] = is_todo_template_placeholder(existing)
        bridge_context = build_openspec_bridge_context(config, tasks_text)
        write_managed_json(config, openspec_bridge_context_path(config), bridge_context)
        return result
    service_names = infer_openspec_service_hints(config, tasks_text)
    bridged_todo = transform_openspec_tasks_to_todo(
        tasks_text,
        str(config.get("openspec", {}).get("change_name", tasks_file.parent.name)),
        openspec_change_dir(config),
        service_names,
    )
    if existing != bridged_todo:
        write_text(todo_file, bridged_todo)
        result["bridge_generated"] = True
    result["bridge_source"] = str(tasks_file)
    result["todo_needs_edit"] = is_todo_template_placeholder(bridged_todo)
    bridge_context = build_openspec_bridge_context(config, tasks_text)
    write_managed_json(config, openspec_bridge_context_path(config), bridge_context)
    return result


def is_todo_template_placeholder(todo_text: str) -> bool:
    normalized = todo_text.strip()
    if not normalized:
        return True
    if normalized == todo_template().strip():
        return True
    markers = [
        "your-service-name",
        "在这里写大需求模块名称",
        "在这里写主任务 1",
        "在这里写需要补充的测试、文档或验证要求",
    ]
    return any(marker in normalized for marker in markers)


def normalize_todo_text_item(text: str) -> str:
    normalized = text.strip()
    normalized = re.sub(r"^\d+\.\s*", "", normalized)
    normalized = re.sub(r"^[-*]\s*", "", normalized)
    normalized = re.sub(r"^\[(?: |x|X)\]\s*", "", normalized)
    return normalized.strip()


def parse_todo_document(todo_text: str) -> dict[str, Any]:
    constraints: list[str] = []
    modules: list[dict[str, Any]] = []
    default_module_title = "未分组需求"
    current_section = "__root__"
    current_module: dict[str, Any] | None = None
    current_task: dict[str, Any] | None = None

    def ensure_module(title: str | None = None) -> dict[str, Any]:
        nonlocal current_module, current_task
        if current_module is not None and title is None:
            return current_module
        module_title = (title or default_module_title).strip() or default_module_title
        if current_module and current_module["title"] == module_title:
            return current_module
        current_module = {
            "id": f"module-{len(modules) + 1}",
            "title": module_title,
            "tasks": [],
        }
        modules.append(current_module)
        current_task = None
        return current_module

    for raw_line in todo_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        heading = re.match(r"^(#+)\s*(.+?)\s*$", stripped)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            lowered = title.lower()
            if level == 1:
                current_task = None
                if is_constraint_section(lowered):
                    current_section = "constraints"
                    current_module = None
                elif is_task_section(lowered):
                    current_section = "tasks"
                    current_module = None
                else:
                    current_section = lowered
                    current_module = None
                continue
            if current_section == "tasks":
                ensure_module(title)
                continue

        if current_section == "constraints":
            normalized = normalize_todo_text_item(stripped)
            if normalized:
                constraints.append(normalized)
            continue

        in_task_area = current_section == "tasks" or current_section == "__root__"
        if not in_task_area:
            continue

        checkbox = re.match(r"^[-*]\s+\[( |x|X)\]\s*(.+)$", stripped)
        if checkbox:
            module = ensure_module()
            completed = checkbox.group(1).lower() == "x"
            title = normalize_todo_text_item(checkbox.group(2))
            current_task = {
                "title": title,
                "details": [],
                "completed": completed,
            }
            module["tasks"].append(current_task)
            continue

        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        if bullet:
            module = ensure_module()
            title = normalize_todo_text_item(bullet.group(1))
            current_task = {
                "title": title,
                "details": [],
                "completed": False,
            }
            module["tasks"].append(current_task)
            continue

        detail = normalize_todo_text_item(stripped)
        if not detail:
            continue
        if current_task is None:
            module = ensure_module()
            current_task = {
                "title": detail,
                "details": [],
                "completed": False,
            }
            module["tasks"].append(current_task)
            continue
        current_task["details"].append(detail)

    normalized_modules: list[dict[str, Any]] = []
    task_index = 1
    for module_index, module in enumerate(modules, start=1):
        normalized_tasks: list[dict[str, Any]] = []
        for task in module.get("tasks", []):
            title = str(task.get("title", "")).strip()
            if not title:
                continue
            details = [str(item).strip() for item in task.get("details", []) if str(item).strip()]
            normalized_tasks.append(
                {
                    "id": f"task-{task_index}",
                    "title": title,
                    "details": details,
                    "completed": bool(task.get("completed", False)),
                }
            )
            task_index += 1
        if normalized_tasks:
            normalized_modules.append(
                {
                    "id": f"module-{module_index}",
                    "title": str(module.get("title", default_module_title)).strip() or default_module_title,
                    "tasks": normalized_tasks,
                }
            )

    pending_modules: list[dict[str, Any]] = []
    completed_modules: list[dict[str, Any]] = []
    for module in normalized_modules:
        pending_tasks = [task for task in module["tasks"] if not task["completed"]]
        completed_tasks = [task for task in module["tasks"] if task["completed"]]
        if pending_tasks:
            pending_modules.append(
                {
                    "id": module["id"],
                    "title": module["title"],
                    "tasks": pending_tasks,
                }
            )
        if completed_tasks:
            completed_modules.append(
                {
                    "id": module["id"],
                    "title": module["title"],
                    "tasks": completed_tasks,
                }
            )

    pending_tasks = [task for module in pending_modules for task in module["tasks"]]
    completed_tasks = [task for module in completed_modules for task in module["tasks"]]
    return {
        "constraints": unique(constraints),
        "modules": pending_modules,
        "completed_modules": completed_modules,
        "tasks": pending_tasks,
        "completed_tasks": completed_tasks,
        "stats": {
            "pending_task_count": len(pending_tasks),
            "completed_task_count": len(completed_tasks),
            "total_task_count": len(pending_tasks) + len(completed_tasks),
        },
    }


def parse_task_blocks(todo_text: str) -> list[dict[str, Any]]:
    document = parse_todo_document(todo_text)
    return [
        {
            "id": task["id"],
            "title": task["title"],
            "details": task["details"],
        }
        for task in document["tasks"]
    ]


def parse_task_modules(todo_text: str) -> list[dict[str, Any]]:
    document = parse_todo_document(todo_text)
    return [
        {
            "id": module["id"],
            "title": module["title"],
            "tasks": [
                {
                    "id": task["id"],
                    "title": task["title"],
                    "details": task["details"],
                }
                for task in module["tasks"]
            ],
        }
        for module in document["modules"]
    ]


def todo_progress(todo_text: str) -> dict[str, int]:
    document = parse_todo_document(todo_text)
    return {
        "pending_task_count": int(document["stats"]["pending_task_count"]),
        "completed_task_count": int(document["stats"]["completed_task_count"]),
        "total_task_count": int(document["stats"]["total_task_count"]),
    }


def todo_items(todo_text: str) -> list[str]:
    tasks = parse_task_blocks(todo_text)
    items: list[str] = []
    for task in tasks:
        items.append(task["title"])
        items.extend(task.get("details", []))
    return unique(items)


def parse_todo_sections(todo_text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"__root__": []}
    current = "__root__"
    for raw_line in todo_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        heading = re.match(r"^#+\s*(.+?)\s*$", stripped)
        if heading:
            current = heading.group(1).strip().lower()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(stripped)
    return sections


def is_task_section(title: str) -> bool:
    return any(keyword in title for keyword in ("待办", "todo", "tasks", "task"))


def is_constraint_section(title: str) -> bool:
    return any(keyword in title for keyword in ("限制条件", "约束", "constraints", "constraint"))


def constraint_items(todo_text: str) -> list[str]:
    return parse_todo_document(todo_text)["constraints"]


def _extract_service_hints_from_lines(lines: list[str]) -> list[str]:
    hints: list[str] = []
    patterns = [
        r"(?:修改的服务|目标服务|服务名|服务)\s*(?:是|为|:|：)\s*([A-Za-z0-9._-]+)",
        r"(?:修改的服务|目标服务|服务名|服务)\s*(?:包括|包含|有|涉及)\s*([A-Za-z0-9._,\-、，\s]+)",
        r"(?:仓库|项目)\s*(?:是|为|:|：)\s*([A-Za-z0-9._-]+)",
        r"(?:仓库|项目)\s*(?:包括|包含|有|涉及)\s*([A-Za-z0-9._,\-、，\s]+)",
    ]
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in patterns:
            match = re.search(pattern, stripped, flags=re.IGNORECASE)
            if not match:
                continue
            raw_value = match.group(1).strip()
            for part in re.split(r"[、，,\s]+", raw_value):
                normalized = part.strip()
                if normalized:
                    hints.append(normalized)
    return unique(hints)


def service_hints(todo_text: str) -> list[str]:
    candidate_lines = constraint_items(todo_text)
    hints = _extract_service_hints_from_lines(candidate_lines)
    if not hints:
        candidate_lines = [line.strip() for line in todo_text.splitlines() if line.strip()]
        hints = _extract_service_hints_from_lines(candidate_lines)
    return unique(hints)


def summarize_todo(todo_text: str) -> str:
    tasks = parse_task_blocks(todo_text)
    if not tasks:
        return "请在 todo 文件中补充待办需求。"
    return "；".join(task["title"] for task in tasks[:3])


def extract_todo_keywords(todo_text: str, limit: int = 24) -> list[str]:
    keywords: list[str] = []
    candidates = todo_items(todo_text) + constraint_items(todo_text)
    token_pattern = re.compile(r"[A-Za-z_][A-Za-z0-9_./:-]{2,}|[\u4e00-\u9fff]{2,}")
    stopwords = {
        "todo",
        "task",
        "tasks",
        "null",
        "true",
        "false",
        "修改",
        "增加",
        "新增",
        "需要",
        "接口",
        "字段",
        "测试",
        "服务",
        "模块",
        "待办",
        "限制条件",
    }
    for candidate in candidates:
        for token in token_pattern.findall(str(candidate)):
            normalized = token.strip("`'\"，。、；：:()（）[]【】")
            if len(normalized) < 2:
                continue
            if normalized.lower() in stopwords or normalized in stopwords:
                continue
            keywords.append(normalized)
    return unique(keywords)[:limit]


def existing_reference_files(config: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for item in config.get("reference_files", []):
        path = Path(str(item))
        if path.exists():
            files.append(str(path.resolve()))
    files.extend(openspec_reference_files(config))
    return unique(files)


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


def workflow_duration_seconds(
    session_meta: dict[str, Any],
    status: dict[str, Any] | None = None,
    finished_at: str | None = None,
) -> float:
    start_text = (
        str((status or {}).get("started_at", "")).strip()
        or str(session_meta.get("started_at", "")).strip()
        or str(session_meta.get("created_at", "")).strip()
    )
    end_text = str(finished_at or (status or {}).get("finished_at", "")).strip() or now_iso()
    started = parse_iso_datetime(start_text)
    ended = parse_iso_datetime(end_text)
    if started is None or ended is None:
        return 0.0
    return max(0.0, (ended - started).total_seconds())


def phase_after(stage: str, mode: str) -> tuple[str, bool, str, str]:
    if stage == "plan":
        if mode == "manual":
            return ("wait_confirm_plan", True, "implement", "等待确认后开始按计划修改代码。")
        return ("plan", False, "", "继续进入实现阶段。")
    if stage == "implement":
        if mode == "manual":
            return ("wait_confirm_implement", True, "review", "等待确认后执行代码审查。")
        return ("implement", False, "", "继续进入代码审查阶段。")
    if stage == "review":
        if mode == "manual":
            return ("wait_confirm_review", True, "verify", "等待确认后执行验证。")
        return ("review", False, "", "继续进入验证阶段。")
    return ("done", False, "", "工作流已完成。")


def scan_java_files(codebase: Path) -> list[str]:
    results: list[str] = []
    for pattern in ("src/main/java/**/*.java", "src/test/java/**/*.java"):
        for path in sorted(codebase.glob(pattern)):
            results.append(str(path.resolve()))
    return results


def infer_java_modules(paths: list[str]) -> list[str]:
    modules: list[str] = []
    for absolute_path in paths:
        parts = Path(absolute_path).parts
        if "java" not in parts:
            continue
        java_index = parts.index("java")
        package_parts = list(parts[java_index + 1 : -1])
        if not package_parts:
            continue
        if len(package_parts) >= 2:
            module = f"{package_parts[-2]}-{package_parts[-1]}"
        else:
            module = package_parts[-1]
        if module not in modules:
            modules.append(module)
    return modules
