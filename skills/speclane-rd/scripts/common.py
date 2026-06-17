#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib.parse import urlparse
from urllib import request as urllib_request


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def parse_iso_datetime(value: str) -> datetime | None:
    normalized = str(value).strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_duration(seconds: float) -> str:
    safe_seconds = max(0.0, float(seconds))
    if safe_seconds < 60:
        return f"{safe_seconds:.2f} 秒"
    total_seconds = int(round(safe_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分")
    if secs or not parts:
        parts.append(f"{secs} 秒")
    return "".join(parts)


def parse_scalar(value: str) -> Any:
    stripped = value.strip()
    if stripped in ("", "null", "~"):
        return ""
    if stripped == "[]":
        return []
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return []
        parts: list[str] = []
        current = ""
        quote = ""
        for char in inner:
            if char in ("'", '"'):
                if not quote:
                    quote = char
                elif quote == char:
                    quote = ""
                current += char
                continue
            if char == "," and not quote:
                parts.append(current.strip())
                current = ""
                continue
            current += char
        if current.strip():
            parts.append(current.strip())
        return [parse_scalar(part) for part in parts]
    if stripped == "true":
        return True
    if stripped == "false":
        return False
    if re.fullmatch(r"-?\d+", stripped):
        return int(stripped)
    if (stripped.startswith('"') and stripped.endswith('"')) or (
        stripped.startswith("'") and stripped.endswith("'")
    ):
        return stripped[1:-1]
    return stripped


def _tokenize_yaml(text: str) -> list[tuple[int, str]]:
    tokens: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        if raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        tokens.append((indent, raw_line.strip()))
    return tokens


def _parse_yaml_block(tokens: list[tuple[int, str]], index: int, indent: int) -> tuple[int, Any]:
    container: Any = None

    while index < len(tokens):
        current_indent, content = tokens[index]
        if current_indent < indent:
            break
        if current_indent > indent:
            raise ValueError(f"YAML 缩进不合法：{content}")

        if content.startswith("- "):
            if container is None:
                container = []
            if not isinstance(container, list):
                raise ValueError("YAML 不能在同一层混用对象和数组。")
            value_part = content[2:].strip()
            if value_part:
                container.append(parse_scalar(value_part))
                index += 1
                continue
            index, nested = _parse_yaml_block(tokens, index + 1, indent + 2)
            container.append(nested)
            continue

        if container is None:
            container = {}
        if not isinstance(container, dict):
            raise ValueError("YAML 不能在同一层混用数组和对象。")

        key, sep, value_part = content.partition(":")
        if not sep:
            raise ValueError(f"YAML 行缺少冒号：{content}")
        key = key.strip()
        value_part = value_part.strip()
        if value_part:
            container[key] = parse_scalar(value_part)
            index += 1
            continue
        index, nested = _parse_yaml_block(tokens, index + 1, indent + 2)
        container[key] = nested

    if container is None:
        return index, {}
    return index, container


def parse_simple_yaml(text: str) -> dict[str, Any]:
    tokens = _tokenize_yaml(text)
    if not tokens:
        return {}
    _, parsed = _parse_yaml_block(tokens, 0, tokens[0][0])
    if not isinstance(parsed, dict):
        raise ValueError("工作空间配置必须是对象结构。")
    return parsed


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


def demand_instance_path(root: Path | str, demand_name: str) -> Path:
    return demand_runtime_dir(root, demand_name) / "demand.yml"


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


def resolve_requested_demand(root: Path, raw_config: dict[str, Any], explicit_demand: str | None = None) -> str:
    requested = str(explicit_demand or "").strip() or str(os.environ.get("SPECLANE_DEMAND_NAME", "")).strip()
    if requested:
        return validate_demand_name(requested)
    active = read_active_demand(root)
    if active:
        return validate_demand_name(active)
    vars_map = raw_config.get("vars", {})
    if isinstance(vars_map, dict):
        configured = str(vars_map.get("demand_name", "")).strip()
        if configured and demand_instance_path(root, configured).exists():
            return validate_demand_name(configured)
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
    config.setdefault("demand_file", f"demands/{demand_name}/需求.md")
    config.setdefault("todo_file", f"demands/{demand_name}/todo.md")
    config.setdefault("output_dir", f"demands/{demand_name}/output")
    return config


def load_demand_instance(root: Path, demand_name: str) -> dict[str, Any]:
    if not demand_name:
        return {}
    path = demand_instance_path(root, demand_name)
    if not path.exists():
        return {}
    loaded = parse_simple_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"需求实例配置必须是对象：{path}")
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
    demand_config = load_demand_instance(root, demand_name)
    merged_raw = dict(raw_config)
    if demand_config:
        for key, value in demand_config.items():
            if key in ("version",):
                continue
            merged_raw[key] = value
    if demand_name:
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
    config["__demand_instance_path"] = str(demand_instance_path(root, demand_name)) if demand_name else ""
    config["__config_path"] = str(config_path)
    config["__skill_root"] = str(skill_root())
    config["__skill_config_path"] = str(skill_config_path())
    config["__skill_config_created"] = bool(skill_config.get("__skill_config_created", False))
    config["__skill_config_example_path"] = str(skill_config_example_path())
    return config


def code_root(config: dict[str, Any]) -> Path:
    return Path(str(config["code_path"])).resolve()


def looks_like_project_root(path: Path) -> bool:
    return any(
        (
            (path / ".git").exists(),
            (path / "pom.xml").exists(),
            (path / "mvnw").exists(),
            (path / "build.gradle").exists(),
            (path / "build.gradle.kts").exists(),
            (path / "gradlew").exists(),
            (path / "package.json").exists(),
            (path / "go.mod").exists(),
            (path / "pyproject.toml").exists(),
            (path / "requirements.txt").exists(),
            (path / "Cargo.toml").exists(),
            (path / "composer.json").exists(),
            (path / "Gemfile").exists(),
            (path / "CMakeLists.txt").exists(),
            (path / "src" / "main").exists(),
        )
    )


def todo_path(config: dict[str, Any]) -> Path:
    return Path(str(config["todo_file"])).resolve()


def workflow_source(config: dict[str, Any]) -> str:
    return str(config.get("workflow_source", "todo")).strip() or "todo"


def artifacts_dir(config: dict[str, Any]) -> Path:
    root = Path(str(config["__workspace_root"])).resolve()
    demand_name = str(config.get("__demand_name", "")).strip()
    if demand_name:
        return demand_runtime_dir(root, demand_name)
    return root / ".speclane"


def active_openspec_change_path(root: Path | str, demand_name: str = "") -> Path:
    selected = str(demand_name).strip()
    if selected:
        return demand_runtime_dir(root, selected) / "current-openspec-change.json"
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
        "workspace": str(workspace_root(Path(str(config["__workspace_root"])))),
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
    write_managed_json(config, current_session_file(config), session_meta)
    return session_meta


def current_session_meta(config: dict[str, Any]) -> dict[str, Any]:
    session_meta = read_json(current_session_file(config), {})
    return _normalize_session_meta(config, session_meta)


def current_session_status(config: dict[str, Any], session_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = session_meta or current_session_meta(config)
    status = read_json(data_artifact_path(config, "status.json", meta), {})
    return status if isinstance(status, dict) else {}


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
    "propose-openspec": "/sl:propose",
    "bootstrap-openspec": "/sl:bridge",
    "plan": "/sl:plan",
    "apply": "/sl:apply",
    "start-implement": "/sl:apply",
    "finish-implement": "/sl:apply",
    "review": "/sl:review",
    "verify": "/sl:verify",
    "prepare-archive-openspec": "/sl:archive-check",
    "archive-openspec": "/sl:archive",
}


SL_COMMAND_TO_RUN_COMMAND: dict[str, str] = {
    "/sl:init": "init",
    "/sl:propose": "propose-openspec",
    "/sl:bridge": "bootstrap-openspec",
    "/sl:plan": "plan",
    "/sl:apply": "apply",
    "/sl:review": "review",
    "/sl:verify": "verify",
    "/sl:archive-check": "prepare-archive-openspec",
    "/sl:archive": "archive-openspec",
    "/sl:status": "status",
    "/sl:recover": "recover",
    "/sl:demand": "demand",
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
    write_managed_json(config, path, state)
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


def openspec_tasks_hash(config: dict[str, Any]) -> str:
    if workflow_source(config) != "openspec":
        return ""
    return file_sha256(openspec_tasks_path(config))


def openspec_artifact_hashes(config: dict[str, Any]) -> dict[str, Any]:
    if workflow_source(config) != "openspec":
        return {}
    result: dict[str, Any] = {}
    openspec = config.get("openspec", {})
    for key in ("proposal_file", "design_file", "tasks_file"):
        path_text = str(openspec.get(key, "")).strip()
        if not path_text:
            continue
        path = Path(path_text)
        if path.exists() and path.is_file():
            result[key.replace("_file", "")] = {
                "path": str(path.resolve()),
                "sha256": file_sha256(path),
            }
    specs: list[dict[str, str]] = []
    specs_dir_text = str(openspec.get("specs_dir", "")).strip()
    if specs_dir_text:
        specs_dir = Path(specs_dir_text)
        if specs_dir.exists() and specs_dir.is_dir():
            for path in sorted(specs_dir.rglob("*.md")):
                specs.append(
                    {
                        "path": str(path.resolve()),
                        "relative_path": str(path.relative_to(specs_dir)),
                        "sha256": file_sha256(path),
                    }
                )
    result["specs"] = specs
    return result


def openspec_hash_drift(config: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    current = openspec_artifact_hashes(config)
    drifts: list[str] = []
    for key in ("proposal", "design", "tasks"):
        old_item = baseline.get(key, {}) if isinstance(baseline, dict) else {}
        new_item = current.get(key, {}) if isinstance(current, dict) else {}
        old_hash = str(old_item.get("sha256", "")).strip() if isinstance(old_item, dict) else ""
        new_hash = str(new_item.get("sha256", "")).strip() if isinstance(new_item, dict) else ""
        if old_hash and new_hash and old_hash != new_hash:
            drifts.append(f"{key}.md 自执行回写后已变化")
    old_specs = {
        str(item.get("relative_path", "")): str(item.get("sha256", ""))
        for item in baseline.get("specs", [])
        if isinstance(item, dict)
    } if isinstance(baseline, dict) else {}
    new_specs = {
        str(item.get("relative_path", "")): str(item.get("sha256", ""))
        for item in current.get("specs", [])
        if isinstance(item, dict)
    } if isinstance(current, dict) else {}
    for rel, old_hash in old_specs.items():
        new_hash = new_specs.get(rel, "")
        if old_hash and new_hash and old_hash != new_hash:
            drifts.append(f"specs/{rel} 自执行回写后已变化")
    return drifts


def _sl_state_artifact_exists(path_text: str) -> bool:
    return bool(path_text and Path(path_text).exists())


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
        if todo_file.exists() and not is_todo_template_placeholder(todo_text):
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
    if workflow_source(config) != "openspec":
        sl_command = RUN_COMMAND_TO_SL_COMMAND.get(run_command, "")
        phase, status, session_meta = _status_phase_for_todo(config)
        allowed_next = TODO_PHASE_ALLOWED_NEXT.get(phase, [])
        errors: list[str] = []
        if run_command in ("propose-openspec", "bootstrap-openspec", "prepare-archive-openspec", "archive-openspec"):
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


def require_sl_state(config: dict[str, Any], run_command: str) -> None:
    result = validate_sl_state(config, run_command)
    if result.get("valid"):
        return
    errors = result.get("errors", [])
    message = "\n".join(str(item) for item in errors) if errors else "当前工作流状态不允许执行该命令。"
    raise SystemExit(message)


def parse_sl_command(text: str) -> dict[str, Any]:
    stripped = str(text).strip()
    match = re.match(r"^(/sl:[a-z][a-z-]*)(?:\s+([A-Za-z0-9][A-Za-z0-9-]*))?(?:\s|$)", stripped)
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
    writeback = openspec_writeback_dir(config).resolve() if workflow_source(config) == "openspec" else None
    allowed_roots = [data_root, output_root]
    if writeback:
        allowed_roots.append(writeback)
        allowed_roots.append(openspec_archive_root(config).resolve())
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise ValueError(f"拒绝写入非工作流托管产物：{resolved}")
    return resolved


def ensure_runtime_dirs(config: dict[str, Any]) -> None:
    artifacts_dir(config).mkdir(parents=True, exist_ok=True)
    sessions_dir(config).mkdir(parents=True, exist_ok=True)
    output_dir(config).mkdir(parents=True, exist_ok=True)


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def file_sha256(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def markdown_headings(text: str, limit: int = 16) -> list[str]:
    headings: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            headings.append(stripped[:160])
        if len(headings) >= limit:
            break
    return headings


def compact_text_excerpt(text: str, keywords: list[str] | None = None, max_chars: int = 6000) -> str:
    normalized = text.strip()
    if len(normalized) <= max_chars:
        return normalized

    keywords = [item.lower() for item in (keywords or []) if item]
    lines = normalized.splitlines()
    selected: list[str] = []
    selected.extend(lines[:60])
    for index, line in enumerate(lines):
        lowered = line.lower()
        if keywords and not any(keyword in lowered for keyword in keywords):
            continue
        start = max(0, index - 2)
        end = min(len(lines), index + 3)
        selected.append("")
        selected.extend(lines[start:end])
        if len("\n".join(selected)) >= max_chars:
            break
    excerpt = "\n".join(selected).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rstrip()
    return excerpt + "\n\n...[已摘要，按需读取原文件全文]..."


def summarize_markdown_file(path: Path, keywords: list[str] | None = None, max_excerpt_chars: int = 6000) -> dict[str, Any]:
    text = read_text(path)
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size if path.exists() else 0,
        "sha256": file_sha256(path),
        "headings": markdown_headings(text),
        "excerpt": compact_text_excerpt(text, keywords=keywords, max_chars=max_excerpt_chars),
        "truncated": len(text.strip()) > max_excerpt_chars,
    }


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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


def openspec_change_dir(config: dict[str, Any]) -> Path:
    return Path(str(config.get("openspec", {}).get("change_dir", ""))).resolve()


def openspec_change_name(config: dict[str, Any]) -> str:
    return str(config.get("openspec", {}).get("change_name", "")).strip()


def validate_openspec_change_name(change_name: str) -> str:
    normalized = str(change_name).strip()
    if not normalized:
        raise ValueError("OpenSpec change 名称不能为空。请使用 /sl:propose <change-name> 显式指定。")
    if "/" in normalized or "\\" in normalized:
        raise ValueError("OpenSpec change 名称不能包含路径分隔符。")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", normalized):
        raise ValueError("OpenSpec change 名称必须匹配 [a-z][a-z0-9-]*，例如 demand-addition-rate。")
    return normalized


def select_openspec_change(config: dict[str, Any], change_name: str) -> dict[str, Any]:
    selected_name = validate_openspec_change_name(change_name)
    openspec = dict(config.get("openspec", {}))
    changes_dir_text = str(openspec.get("changes_dir", "")).strip()
    if changes_dir_text:
        changes_dir = Path(changes_dir_text).resolve()
    else:
        current_dir = Path(str(openspec.get("change_dir", ""))).resolve()
        changes_dir = current_dir if current_dir.name == "changes" else current_dir.parent
    change_dir = changes_dir / selected_name
    openspec.update(
        {
            "changes_dir": str(changes_dir),
            "change_name": selected_name,
            "change_dir": str(change_dir),
            "tasks_file": str(change_dir / "tasks.md"),
            "proposal_file": str(change_dir / "proposal.md"),
            "design_file": str(change_dir / "design.md"),
            "specs_dir": str(change_dir / "specs"),
            "writeback_dir": str(change_dir / "speclane"),
        }
    )
    config["openspec"] = openspec
    return config


def write_active_openspec_change(config: dict[str, Any], change_name: str) -> Path:
    selected_name = validate_openspec_change_name(change_name)
    active_path = active_openspec_change_path(config["__workspace_root"], str(config.get("__demand_name", "")))
    active_path.parent.mkdir(parents=True, exist_ok=True)
    write_managed_json(
        config,
        active_path,
        {
            "change_name": selected_name,
            "change_dir": str(openspec_change_dir(config)),
            "updated_at": now_iso(),
        },
    )
    return active_path


def openspec_root(config: dict[str, Any]) -> Path:
    changes_dir = str(config.get("openspec", {}).get("changes_dir", "")).strip()
    if changes_dir:
        resolved = Path(changes_dir).resolve()
        if resolved.name == "changes" and resolved.parent.name == "openspec":
            return resolved.parent.parent.resolve()
        return resolved.parent.resolve()
    change_dir = openspec_change_dir(config)
    if change_dir.parent.name == "changes" and change_dir.parent.parent.name == "openspec":
        return change_dir.parent.parent.parent.resolve()
    return change_dir.parent.parent.resolve()


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


def openspec_cli_available() -> bool:
    return bool(shutil.which("openspec"))


def run_openspec_cli(config: dict[str, Any], args: list[str]) -> dict[str, Any]:
    if not openspec_cli_available():
        return {
            "available": False,
            "args": args,
            "returncode": None,
            "stdout": "",
            "stderr": "openspec CLI not found in PATH",
            "json": None,
        }
    result = subprocess.run(
        ["openspec", *args],
        cwd=str(openspec_root(config)),
        text=True,
        capture_output=True,
        check=False,
    )
    parsed_json: Any = None
    stdout = result.stdout.strip()
    if stdout:
        try:
            parsed_json = json.loads(stdout)
        except json.JSONDecodeError:
            parsed_json = None
    return {
        "available": True,
        "args": args,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "json": parsed_json,
    }


def collect_openspec_cli_context(config: dict[str, Any], include_apply: bool = False, include_archive: bool = False) -> dict[str, Any]:
    change_name = openspec_change_name(config)
    context: dict[str, Any] = {
        "available": openspec_cli_available(),
        "change_name": change_name,
        "status": {},
        "apply_instructions": {},
        "archive_instructions": {},
    }
    if not change_name:
        context["error"] = "missing active OpenSpec change; run /sl:propose <change-name> first"
        return context
    if not context["available"]:
        context["error"] = "openspec CLI not found in PATH"
        return context
    status = run_openspec_cli(config, ["status", "--change", change_name, "--json"])
    context["status"] = status
    if include_apply:
        context["apply_instructions"] = run_openspec_cli(config, ["instructions", "apply", "--change", change_name, "--json"])
    if include_archive:
        context["archive_instructions"] = run_openspec_cli(config, ["instructions", "archive", "--change", change_name, "--json"])
    return context


def openspec_tasks_path(config: dict[str, Any]) -> Path:
    return Path(str(config.get("openspec", {}).get("tasks_file", ""))).resolve()


def openspec_reference_files(config: dict[str, Any]) -> list[str]:
    if workflow_source(config) != "openspec":
        return []
    openspec = config.get("openspec", {})
    files: list[str] = []
    for key in ("proposal_file", "design_file"):
        path_text = str(openspec.get(key, "")).strip()
        if not path_text:
            continue
        path = Path(path_text)
        if path.exists() and path.is_file():
            files.append(str(path.resolve()))
    specs_dir_text = str(openspec.get("specs_dir", "")).strip()
    if specs_dir_text:
        specs_dir = Path(specs_dir_text)
        if specs_dir.exists() and specs_dir.is_dir():
            for path in sorted(specs_dir.rglob("*.md")):
                files.append(str(path.resolve()))
    return unique(files)


def openspec_writeback_dir(config: dict[str, Any]) -> Path:
    return Path(str(config.get("openspec", {}).get("writeback_dir", ""))).resolve()


def openspec_archive_root(config: dict[str, Any]) -> Path:
    return openspec_change_dir(config).parent / "archive"


def openspec_bridge_context_path(config: dict[str, Any]) -> Path:
    return artifacts_dir(config) / "openspec-bridge-context.json"


def transform_openspec_tasks_to_todo(tasks_text: str, change_name: str, change_dir: Path, service_names: list[str] | None = None) -> str:
    lines = [
        "# 限制条件",
        f"- 需求来源是 OpenSpec change：{change_name}",
        f"- OpenSpec 变更目录是 {change_dir}",
        "- 优先以 proposal.md、design.md 和 specs/ 下的 delta specs 作为业务边界",
    ]
    for service_name in unique(service_names or []):
        lines.append(f"- 修改的服务是 {service_name}")
    lines.extend(
        [
        "",
        "# 待办事项",
        "",
        ]
    )
    has_tasks = False
    for raw_line in tasks_text.splitlines():
        stripped = raw_line.rstrip()
        compact = stripped.strip()
        if not compact:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if compact.startswith("#"):
            heading_text = compact.lstrip("#").strip()
            if heading_text.lower() == "tasks":
                continue
            lines.append(f"## {heading_text}")
            has_tasks = True
            continue
        if compact.startswith("- [") or compact.startswith("* ["):
            lines.append(compact.replace("* [", "- [", 1))
            has_tasks = True
            continue
        if compact.startswith("- ") or compact.startswith("* "):
            lines.append("- [ ] " + normalize_todo_text_item(compact))
            has_tasks = True
            continue
        if re.match(r"^\d+\.\s+", compact):
            lines.append(compact)
            has_tasks = True
            continue
        lines.append("- [ ] " + normalize_todo_text_item(compact))
        has_tasks = True
    if not has_tasks:
        lines.extend(
            [
                "## 默认任务",
                "- [ ] OpenSpec tasks.md 中未识别到可执行任务，请先补充任务项",
            ]
        )
    if lines[-1] != "":
        lines.append("")
    return "\n".join(lines)


def openspec_source_texts(config: dict[str, Any], tasks_text: str) -> list[str]:
    openspec = config.get("openspec", {})
    proposal_file = Path(str(openspec.get("proposal_file", "")))
    design_file = Path(str(openspec.get("design_file", "")))
    proposal_text = read_text(proposal_file) if proposal_file.exists() else ""
    design_text = read_text(design_file) if design_file.exists() else ""
    texts = [tasks_text, proposal_text, design_text]
    specs_dir = Path(str(openspec.get("specs_dir", ""))).expanduser()
    if specs_dir.exists() and specs_dir.is_dir():
        for path in sorted(specs_dir.rglob("*.md")):
            texts.append(read_text(path))
    return texts


def infer_openspec_service_hints(config: dict[str, Any], tasks_text: str) -> list[str]:
    source_text = "\n".join(openspec_source_texts(config, tasks_text))
    hints = _extract_service_hints_from_lines(source_text.splitlines())
    try:
        root = code_root(config)
        candidates = [root] if looks_like_project_root(root) else find_candidate_codebases(root)
        lowered_source = source_text.lower()
        for candidate in candidates:
            candidate_name = candidate.name.strip()
            if candidate_name and candidate_name.lower() in lowered_source:
                hints.append(candidate_name)
    except Exception:
        pass
    return unique(hints)


def build_openspec_bridge_context(config: dict[str, Any], tasks_text: str) -> dict[str, Any]:
    openspec = config.get("openspec", {})
    proposal_file = Path(str(openspec.get("proposal_file", "")))
    design_file = Path(str(openspec.get("design_file", "")))
    proposal_text = read_text(proposal_file) if proposal_file.exists() else ""
    design_text = read_text(design_file) if design_file.exists() else ""
    specs = openspec_reference_files(config)
    change_dir = openspec_change_dir(config)
    repo_openspec_root = change_dir.parent.parent
    delta_specs_dir = Path(str(openspec.get("specs_dir", ""))).expanduser()
    spec_merge_targets: list[dict[str, Any]] = []
    if delta_specs_dir.exists() and delta_specs_dir.is_dir():
        target_specs_root = repo_openspec_root / "specs"
        for source in sorted(delta_specs_dir.rglob("*.md")):
            relative = source.relative_to(delta_specs_dir)
            target = target_specs_root / relative
            spec_merge_targets.append(
                {
                    "delta_source": str(source.resolve()),
                    "target": str(target.resolve()),
                    "relative_path": str(relative),
                    "target_exists": target.exists(),
                    "target_sha256": file_sha256(target),
                }
            )

    def extract_headings(text: str) -> list[str]:
        headings: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                headings.append(stripped.lstrip("#").strip())
        return headings[:12]

    compatibility_notes: list[str] = []
    for source_text in (proposal_text, design_text, tasks_text):
        for line in source_text.splitlines():
            stripped = line.strip()
            lowered = stripped.lower()
            if any(keyword in lowered for keyword in ("兼容", "rollback", "回滚", "灰度", "mq", "schema", "contract")):
                compatibility_notes.append(stripped)

    acceptance_criteria: list[str] = []
    for line in tasks_text.splitlines():
        stripped = normalize_todo_text_item(line)
        if stripped and ("test" in stripped.lower() or "验证" in stripped or "验收" in stripped):
            acceptance_criteria.append(stripped)

    return {
        "workflow_source": "openspec",
        "change_name": openspec_change_name(config),
        "change_dir": str(openspec_change_dir(config)),
        "tasks_file": str(openspec_tasks_path(config)),
        "openspec_cli": collect_openspec_cli_context(config, include_apply=True),
        "proposal_file": str(proposal_file.resolve()) if proposal_file.exists() else "",
        "design_file": str(design_file.resolve()) if design_file.exists() else "",
        "spec_reference_files": specs,
        "proposal_headings": extract_headings(proposal_text),
        "design_headings": extract_headings(design_text),
        "proposal_excerpt": compact_text_excerpt(proposal_text, max_chars=3000) if proposal_text else "",
        "design_excerpt": compact_text_excerpt(design_text, max_chars=3000) if design_text else "",
        "business_constraints": [
            f"需求来源是 OpenSpec change：{openspec.get('change_name', '')}",
            "优先以 proposal.md、design.md 和 specs/ 下的 delta specs 作为业务边界",
        ],
        "service_hints": infer_openspec_service_hints(config, tasks_text),
        "acceptance_criteria": unique(acceptance_criteria),
        "compatibility_notes": unique(compatibility_notes),
        "spec_merge_targets": spec_merge_targets,
        "updated_at": now_iso(),
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


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return fallback
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def write_managed_json(config: dict[str, Any], path: Path, payload: Any) -> None:
    write_json(assert_managed_artifact(path, config), payload)


def write_managed_text(config: dict[str, Any], path: Path, content: str) -> None:
    write_text(assert_managed_artifact(path, config), content)


def unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


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


def relative_to(path: str | Path, root: Path) -> str:
    candidate = Path(str(path)).resolve()
    try:
        return str(candidate.relative_to(root.resolve()))
    except ValueError:
        return str(candidate)


def existing_reference_files(config: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for item in config.get("reference_files", []):
        path = Path(str(item))
        if path.exists():
            files.append(str(path.resolve()))
    files.extend(openspec_reference_files(config))
    return unique(files)


def find_candidate_codebases(root: Path, max_depth: int = 3) -> list[Path]:
    candidates: list[Path] = []
    if looks_like_project_root(root):
        candidates.append(root.resolve())

    def walk(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = sorted([child for child in path.iterdir() if child.is_dir()])
        except OSError:
            return
        for child in children:
            if child.name.startswith("."):
                continue
            if looks_like_project_root(child):
                candidates.append(child.resolve())
                continue
            walk(child, depth + 1)

    walk(root, 1)
    ordered: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item.resolve())
        if key not in seen:
            seen.add(key)
            ordered.append(item.resolve())
    return ordered


def _match_score(candidate: Path, hint: str) -> int:
    candidate_name = candidate.name.lower()
    candidate_path = str(candidate).lower()
    normalized_hint = hint.strip().lower()
    if not normalized_hint:
        return 0
    if candidate_name == normalized_hint:
        return 100
    if candidate_name.replace("_", "-") == normalized_hint.replace("_", "-"):
        return 95
    if normalized_hint in candidate_name:
        return 85
    if f"/{normalized_hint}/" in candidate_path:
        return 75
    return 0


def resolve_target_codebases(config: dict[str, Any], todo_text: str | None = None) -> tuple[list[Path], dict[str, Any]]:
    root = code_root(config)
    todo_text = todo_text if todo_text is not None else read_text(todo_path(config))
    hints = service_hints(todo_text)

    if looks_like_project_root(root):
        return [root], {
            "configured_code_path": str(root),
            "resolved_code_paths": [str(root)],
            "service_hints": hints,
            "selection_reason": "code_path 本身就是可识别的项目根目录，按单仓模式处理。",
            "candidate_codebases": [str(root)],
        }

    candidates = find_candidate_codebases(root)
    if not candidates:
        raise ValueError(f"code_path 下未找到可识别的项目目录：{root}")
    if not hints:
        lowered_todo = todo_text.lower()
        for candidate in candidates:
            candidate_name = candidate.name.strip()
            if candidate_name and candidate_name.lower() in lowered_todo:
                hints.append(candidate_name)
        hints = unique(hints)

    matched_candidates: list[Path] = []
    matched_pairs: list[dict[str, str]] = []
    for candidate in candidates:
        local_best_hint = ""
        local_best_score = 0
        for hint in hints:
            score = _match_score(candidate, hint)
            if score > local_best_score:
                local_best_score = score
                local_best_hint = hint
        if local_best_score > 0:
            matched_candidates.append(candidate.resolve())
            matched_pairs.append(
                {
                    "service_hint": local_best_hint,
                    "resolved_code_path": str(candidate.resolve()),
                }
            )

    if matched_candidates:
        return matched_candidates, {
            "configured_code_path": str(root),
            "resolved_code_paths": [str(item.resolve()) for item in matched_candidates],
            "service_hints": hints,
            "matched_services": matched_pairs,
            "selection_reason": "根据 todo 中识别到的服务标识，匹配到一个或多个目标项目目录。",
            "candidate_codebases": [str(item) for item in candidates],
        }

    if len(candidates) == 1:
        return [candidates[0].resolve()], {
            "configured_code_path": str(root),
            "resolved_code_paths": [str(candidates[0].resolve())],
            "service_hints": hints,
            "selection_reason": "code_path 下只发现一个可识别的项目目录，已自动使用该目录。",
            "candidate_codebases": [str(item) for item in candidates],
        }

    candidate_names = ", ".join(item.name for item in candidates[:10])
    raise ValueError(
        "code_path 下发现多个项目目录，但无法根据 todo 判断目标服务。"
        f" 请在 todo 中明确写出服务名，例如“修改的服务是 xxx”。候选目录：{candidate_names}"
    )


def resolve_target_codebase(config: dict[str, Any], todo_text: str | None = None) -> tuple[Path, dict[str, Any]]:
    codebases, resolution = resolve_target_codebases(config, todo_text)
    return codebases[0], resolution


def planned_codebases(config: dict[str, Any], session_meta: dict[str, Any] | None = None) -> list[Path]:
    meta = session_meta or current_session_meta(config)
    plan = read_json(data_artifact_path(config, "plan.json", meta), {})
    paths = plan.get("resolved_code_paths", [])
    if isinstance(paths, list) and paths:
        return [Path(str(item)).resolve() for item in paths if str(item).strip()]
    path = str(plan.get("resolved_code_path", "")).strip()
    if path:
        return [Path(path).resolve()]
    return [code_root(config)]


def planned_codebase(config: dict[str, Any], session_meta: dict[str, Any] | None = None) -> Path:
    return planned_codebases(config, session_meta)[0]


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_file_lower(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return ""


def _npm_run_command(manager: str, script: str) -> str:
    if manager == "npm":
        return f"npm run {script}" if script not in ("test", "start") else f"npm {script}"
    if manager == "yarn":
        return f"yarn {script}"
    if manager == "bun":
        return f"bun run {script}"
    return f"{manager} {script}"


def _node_package_manager(codebase: Path) -> str:
    if (codebase / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (codebase / "yarn.lock").exists():
        return "yarn"
    if (codebase / "bun.lockb").exists() or (codebase / "bun.lock").exists():
        return "bun"
    return "npm"


def _node_language_and_tool(codebase: Path, package_json: dict[str, Any], manager: str) -> tuple[str, str]:
    deps: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        value = package_json.get(key, {})
        if isinstance(value, dict):
            deps.update(value)
    has_typescript = (
        "typescript" in deps
        or (codebase / "tsconfig.json").exists()
        or any(codebase.glob("src/**/*.ts"))
        or any(codebase.glob("src/**/*.tsx"))
    )
    framework = ""
    for name in ("vue", "react", "next", "nuxt", "svelte", "angular"):
        if name in deps:
            framework = name
            break
    language = "typescript" if has_typescript else "javascript"
    build_tool = f"{manager}/{framework}" if framework else manager
    return language, build_tool


def _node_commands(codebase: Path, package_json: dict[str, Any], manager: str) -> tuple[str, str, str]:
    scripts = package_json.get("scripts", {})
    scripts = scripts if isinstance(scripts, dict) else {}
    script_names = {str(key): str(value) for key, value in scripts.items()}

    def has_real_script(name: str) -> bool:
        value = script_names.get(name, "").lower()
        if not value:
            return False
        return "no test specified" not in value and "exit 1" not in value

    test_command = ""
    for name in ("test", "test:unit", "unit", "vitest", "jest"):
        if has_real_script(name):
            test_command = _npm_run_command(manager, name)
            break
    build_command = _npm_run_command(manager, "build") if has_real_script("build") else ""
    lint_command = _npm_run_command(manager, "lint") if has_real_script("lint") else ""
    start_command = ""
    for name in ("dev", "start", "serve"):
        if has_real_script(name):
            start_command = _npm_run_command(manager, name)
            break
    if test_command and build_command:
        verify_command = f"{test_command} && {build_command}"
    else:
        verify_command = test_command or lint_command or build_command
    return test_command, start_command, verify_command


def _python_commands(codebase: Path) -> tuple[str, str, str]:
    uses_uv = (codebase / "uv.lock").exists()
    uses_poetry = (codebase / "poetry.lock").exists() or "tool.poetry" in _read_file_lower(codebase / "pyproject.toml")
    has_pytest = (
        (codebase / "pytest.ini").exists()
        or (codebase / "conftest.py").exists()
        or "pytest" in _read_file_lower(codebase / "pyproject.toml")
        or "pytest" in _read_file_lower(codebase / "requirements.txt")
        or (codebase / "tests").exists()
    )
    prefix = "uv run " if uses_uv else "poetry run " if uses_poetry else ""
    if has_pytest:
        test_command = f"{prefix}python -m pytest"
    else:
        test_command = f"{prefix}python -m unittest discover"
    start_command = ""
    if (codebase / "manage.py").exists():
        start_command = f"{prefix}python manage.py runserver"
    elif (codebase / "app.py").exists():
        start_command = f"{prefix}python app.py"
    return test_command, start_command, test_command


def _apply_verify_override(config: dict[str, Any] | None, codebase: Path, detected: dict[str, str]) -> dict[str, str]:
    if not config:
        return detected
    commands = config.get("verify_commands", {})
    if not isinstance(commands, dict) or not commands:
        return detected
    candidates = [
        str(codebase.resolve()),
        str(codebase),
        codebase.name,
        "default",
    ]
    for candidate in candidates:
        command = str(commands.get(candidate, "")).strip()
        if command:
            overridden = dict(detected)
            overridden["verify_command"] = command
            if not overridden.get("test_command"):
                overridden["test_command"] = command
            return overridden
    return detected


def load_project_adapters() -> list[dict[str, Any]]:
    adapters_dir = skill_root() / "adapters"
    if not adapters_dir.exists():
        return []
    adapters: list[dict[str, Any]] = []
    for path in sorted(adapters_dir.glob("*.yml")):
        try:
            adapter = parse_simple_yaml(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(adapter, dict):
            continue
        adapter["__path"] = str(path)
        adapters.append(adapter)
    return adapters


def adapter_matches(codebase: Path, adapter: dict[str, Any]) -> bool:
    detect_files = adapter.get("detect_files", [])
    if isinstance(detect_files, str):
        detect_files = [detect_files]
    if not isinstance(detect_files, list) or not detect_files:
        return False
    return any((codebase / str(item)).exists() for item in detect_files if str(item).strip())


def adapter_detection(codebase: Path) -> dict[str, str]:
    for adapter in load_project_adapters():
        if not adapter_matches(codebase, adapter):
            continue
        return {
            "adapter_id": str(adapter.get("id", "")).strip(),
            "language": str(adapter.get("language", "")).strip(),
            "build_tool": str(adapter.get("build_tool", "")).strip(),
            "test_command": str(adapter.get("test_command", "")).strip(),
            "start_command": str(adapter.get("start_command", "")).strip(),
            "verify_command": str(adapter.get("verify_command", "")).strip(),
            "review_profile": str(adapter.get("review_profile", "")).strip(),
        }
    return {}


def detect_project(codebase: Path, config: dict[str, Any] | None = None) -> dict[str, str]:
    adapter = adapter_detection(codebase)
    has_maven = (codebase / "pom.xml").exists() or (codebase / "mvnw").exists()
    has_gradle = (
        (codebase / "build.gradle").exists()
        or (codebase / "build.gradle.kts").exists()
        or (codebase / "gradlew").exists()
    )
    has_java = has_maven or has_gradle or (codebase / "src" / "main" / "java").exists()
    has_node = (codebase / "package.json").exists()
    has_go = (codebase / "go.mod").exists()
    has_python = any(
        (codebase / name).exists()
        for name in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile", "tox.ini")
    ) or (codebase / "tests").exists()
    has_rust = (codebase / "Cargo.toml").exists()
    has_dotnet = bool(list(codebase.glob("*.sln")) or list(codebase.glob("*.csproj")))
    has_php = (codebase / "composer.json").exists()
    has_ruby = (codebase / "Gemfile").exists() or (codebase / "Rakefile").exists()
    has_make = (codebase / "Makefile").exists() or (codebase / "makefile").exists()
    has_cmake = (codebase / "CMakeLists.txt").exists()

    language = "java" if has_java else "unknown"
    build_tool = ""
    test_command = ""
    start_command = ""
    verify_command = ""

    if has_maven:
        build_tool = "maven"
        test_command = "./mvnw test" if (codebase / "mvnw").exists() else "mvn test"
        start_command = "./mvnw spring-boot:run" if (codebase / "mvnw").exists() else "mvn spring-boot:run"
        verify_command = test_command
    elif has_gradle:
        build_tool = "gradle"
        test_command = "./gradlew test" if (codebase / "gradlew").exists() else "gradle test"
        start_command = "./gradlew bootRun" if (codebase / "gradlew").exists() else "gradle bootRun"
        verify_command = test_command
    elif has_node:
        package_json = _read_json_file(codebase / "package.json")
        manager = _node_package_manager(codebase)
        language, build_tool = _node_language_and_tool(codebase, package_json, manager)
        test_command, start_command, verify_command = _node_commands(codebase, package_json, manager)
    elif has_go:
        language = "go"
        build_tool = "go"
        test_command = "go test ./..."
        start_command = "go run ."
        verify_command = test_command
    elif has_python:
        language = "python"
        build_tool = "uv" if (codebase / "uv.lock").exists() else "poetry" if (codebase / "poetry.lock").exists() else "python"
        test_command, start_command, verify_command = _python_commands(codebase)
    elif has_rust:
        language = "rust"
        build_tool = "cargo"
        test_command = "cargo test"
        start_command = "cargo run"
        verify_command = test_command
    elif has_dotnet:
        language = "csharp"
        build_tool = "dotnet"
        test_command = "dotnet test"
        start_command = "dotnet run"
        verify_command = test_command
    elif has_php:
        language = "php"
        build_tool = "composer"
        composer_json = _read_json_file(codebase / "composer.json")
        scripts = composer_json.get("scripts", {}) if isinstance(composer_json, dict) else {}
        if isinstance(scripts, dict) and scripts.get("test"):
            test_command = "composer test"
        elif (codebase / "vendor" / "bin" / "phpunit").exists() or (codebase / "phpunit.xml").exists():
            test_command = "vendor/bin/phpunit"
        start_command = "php -S localhost:8000 -t public" if (codebase / "public").exists() else ""
        verify_command = test_command
    elif has_ruby:
        language = "ruby"
        build_tool = "bundler"
        if (codebase / "spec").exists():
            test_command = "bundle exec rspec"
        elif (codebase / "test").exists():
            test_command = "bundle exec rake test"
        start_command = "bundle exec rails server" if (codebase / "config" / "application.rb").exists() else ""
        verify_command = test_command
    elif has_make:
        language = "native"
        build_tool = "make"
        makefile = _read_file_lower(codebase / "Makefile") or _read_file_lower(codebase / "makefile")
        test_command = "make test" if re.search(r"^test\s*:", makefile, flags=re.MULTILINE) else ""
        verify_command = test_command or "make"
    elif has_cmake:
        language = "cpp"
        build_tool = "cmake"
        test_command = "ctest --test-dir build" if (codebase / "build").exists() else ""
        verify_command = test_command

    detected = {
        "adapter_id": adapter.get("adapter_id", ""),
        "language": language,
        "build_tool": build_tool,
        "test_command": test_command,
        "start_command": start_command,
        "verify_command": verify_command,
        "review_profile": adapter.get("review_profile", ""),
    }
    for key in ("language", "build_tool", "test_command", "start_command", "verify_command"):
        if not detected.get(key) and adapter.get(key):
            detected[key] = adapter[key]
    return _apply_verify_override(config, codebase, detected)


def summarize_detected_projects(projects: list[dict[str, str]]) -> dict[str, str]:
    if not projects:
        return {
            "adapter_id": "",
            "language": "",
            "build_tool": "",
            "test_command": "",
            "start_command": "",
            "verify_command": "",
            "review_profile": "",
        }

    def summarize_field(name: str) -> str:
        values = unique([item.get(name, "") for item in projects if item.get(name, "")])
        if not values:
            return ""
        if len(values) == 1:
            return values[0]
        return "multiple"

    return {
        "adapter_id": summarize_field("adapter_id"),
        "language": summarize_field("language"),
        "build_tool": summarize_field("build_tool"),
        "test_command": summarize_field("test_command"),
        "start_command": summarize_field("start_command"),
        "verify_command": summarize_field("verify_command"),
        "review_profile": summarize_field("review_profile"),
    }


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


def pushplus_config(config: dict[str, Any]) -> dict[str, Any]:
    notification = config.get("notification", {})
    if not isinstance(notification, dict):
        return {
            "token": "",
            "routes": [],
        }
    pushplus = notification.get("pushplus", {})
    if not isinstance(pushplus, dict):
        return {
            "token": "",
            "routes": [],
        }

    ordinary = pushplus.get("ordinary", {})
    if not isinstance(ordinary, dict):
        ordinary = {}
    return {
        "token": str(pushplus.get("token", "")).strip(),
        "routes": [
            {
                "name": "ordinary",
                "enabled": bool(ordinary.get("enabled", False)),
                "channel": str(ordinary.get("channel", "wechat")).strip() or "wechat",
                "template": str(ordinary.get("template", "markdown")).strip() or "markdown",
            },
        ],
    }


def feishu_config(config: dict[str, Any]) -> dict[str, Any]:
    notification = config.get("notification", {})
    if not isinstance(notification, dict):
        return {
            "enabled": False,
            "webhook_url": "",
            "secret": "",
        }
    feishu = notification.get("feishu", {})
    if not isinstance(feishu, dict):
        return {
            "enabled": False,
            "webhook_url": "",
            "secret": "",
        }
    return {
        "enabled": bool(feishu.get("enabled", False)),
        "webhook_url": str(feishu.get("webhook_url", "")).strip(),
        "secret": str(feishu.get("secret", "")).strip(),
    }


def pushplus_api_url() -> str:
    return str(os.environ.get("SUPER_ENGINEER_PUSHPLUS_URL", "https://www.pushplus.plus/send")).strip()


def pushplus_request_url(token: str) -> str:
    base = pushplus_api_url().rstrip("/")
    return f"{base}/{token}" if token else base


def _normalize_pushplus_response(status_code: int, body: str, route: dict[str, Any], sender: str) -> dict[str, Any]:
    try:
        response_payload = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        response_payload = {"raw": body}
    response_code = response_payload.get("code")
    success = status_code == 200 and response_code in (None, 0, 200, "0", "200")
    return {
        "route": route.get("name", ""),
        "channel": route.get("channel", ""),
        "template": route.get("template", ""),
        "sender": sender,
        "status": "sent" if success else "failed",
        "success": success,
        "message": str(response_payload.get("msg") or ("发送成功" if success else "发送失败")),
        "response": response_payload,
    }


def send_pushplus_notification_python(payload: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    request = urllib_request.Request(
        pushplus_api_url(),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = getattr(response, "status", 200)
    except urllib_error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        result = _normalize_pushplus_response(error.code, body, route, "python")
        result["message"] = str(result.get("message") or f"PushPlus HTTP {error.code}")
        return result
    except urllib_error.URLError as error:
        return {
            "route": route.get("name", ""),
            "channel": route.get("channel", ""),
            "template": route.get("template", ""),
            "sender": "python",
            "status": "failed",
            "success": False,
            "message": f"PushPlus 请求失败：{error.reason}",
            "response": {},
        }
    return _normalize_pushplus_response(status_code, body, route, "python")


def send_pushplus_notification_curl(payload: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    curl_path = shutil.which("curl")
    if not curl_path:
        return {
            "route": route.get("name", ""),
            "channel": route.get("channel", ""),
            "template": route.get("template", ""),
            "sender": "curl",
            "status": "failed",
            "success": False,
            "message": "系统中未找到 curl，无法执行回退发送。",
            "response": {},
        }
    result = subprocess.run(
        [
            curl_path,
            "-sS",
            pushplus_api_url(),
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(payload, ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        return {
            "route": route.get("name", ""),
            "channel": route.get("channel", ""),
            "template": route.get("template", ""),
            "sender": "curl",
            "status": "failed",
            "success": False,
            "message": f"curl 回退发送失败：{result.stderr.strip() or result.stdout.strip() or result.returncode}",
            "response": {},
        }
    return _normalize_pushplus_response(200, result.stdout, route, "curl")


def send_pushplus_notification(pushplus: dict[str, Any], route: dict[str, Any], title: str, content: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "token": str(pushplus.get("token", "")).strip(),
        "title": title,
        "content": content,
        "template": route.get("template", "markdown"),
        "channel": route.get("channel", "wechat"),
    }
    python_result = send_pushplus_notification_python(payload, route)
    if python_result.get("success"):
        return python_result
    curl_result = send_pushplus_notification_curl(payload, route)
    if curl_result.get("success"):
        curl_result["message"] = f"{curl_result.get('message', '')}（Python失败后已回退curl成功）"
        curl_result["python_error"] = python_result.get("message", "")
        return curl_result
    curl_result["python_error"] = python_result.get("message", "")
    curl_result["message"] = (
        f"Python发送失败：{python_result.get('message', '')}；"
        f"curl回退也失败：{curl_result.get('message', '')}"
    )
    return curl_result


def _normalize_feishu_response(status_code: int, body: str, sender: str) -> dict[str, Any]:
    try:
        response_payload = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        response_payload = {"raw": body}
    response_code = response_payload.get("code")
    success = status_code == 200 and response_code in (None, 0, "0")
    return {
        "route": "feishu",
        "channel": "webhook",
        "template": "interactive",
        "sender": sender,
        "status": "sent" if success else "failed",
        "success": success,
        "message": str(response_payload.get("msg") or ("success" if success else "发送失败")),
        "response": response_payload,
    }


def feishu_sign(secret: str, timestamp: str) -> str:
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _workflow_notification_title(session_id: str, overall_result: str) -> str:
    return "SpecLane RD workflow notification"


def _workflow_notification_status_text(status: dict[str, Any], overall_result: str) -> str:
    current_task = str(status.get("current_task", "") or "暂无").strip()
    next_action = str(status.get("next_action", "") or "工作流已完成，请前往工作区查看。").strip()
    if overall_result == "通过":
        current_task = current_task.replace("✅", "").replace("❌", "").rstrip("。")
        return f"{current_task} ✅。{next_action}"
    current_task = current_task.replace("✅", "").replace("❌", "").rstrip("。")
    return f"{current_task} ❌。{next_action}"


def workflow_notification_fingerprint(
    session_meta: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
) -> str:
    finished_at = str(status.get("finished_at", "") or "").strip()
    phase = str(status.get("phase", "") or "").strip()
    current_task = str(status.get("current_task", "") or "").strip()
    return "|".join(
        [
            str(session_meta.get("session_id", "")).strip(),
            overall_result.strip(),
            finished_at,
            phase,
            current_task,
        ]
    )


def notification_has_sent_route(notification: dict[str, Any], route: str, template: str) -> bool:
    results = notification.get("results", [])
    if not isinstance(results, list):
        return False
    for item in results:
        if not isinstance(item, dict):
            continue
        if str(item.get("route", "")).strip() != route:
            continue
        if str(item.get("template", "")).strip() != template:
            continue
        if str(item.get("status", "")).strip() == "sent" and item.get("success", True) is not False:
            return True
    return False


def is_standard_workflow_notification(
    config: dict[str, Any],
    session_meta: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
    notification: dict[str, Any],
) -> bool:
    if not isinstance(notification, dict):
        return False
    if str(notification.get("provider", "")).strip() != "notification":
        return False
    if str(notification.get("source", "")).strip() != "run-workflow.py verify":
        return False
    if str(notification.get("status", "")).strip() not in ("sent", "partial"):
        return False
    expected_fingerprint = workflow_notification_fingerprint(session_meta, status, overall_result)
    if str(notification.get("fingerprint", "")).strip() != expected_fingerprint:
        return False
    if feishu_config(config).get("enabled"):
        return notification_has_sent_route(notification, "feishu", "interactive")
    return str(notification.get("status", "")).strip() == "sent"


def build_workflow_notification(
    config: dict[str, Any],
    session_meta: dict[str, Any],
    plan: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
    template: str = "markdown",
) -> tuple[str, str]:
    duration_seconds = workflow_duration_seconds(session_meta, status, status.get("finished_at", ""))
    progress = plan.get("todo_progress", {})
    targets = [str(item.get("name", "")).strip() for item in plan.get("target_codebases", []) if str(item.get("name", "")).strip()]
    target_text = "、".join(targets) if targets else "未识别"
    title = _workflow_notification_title(session_meta["session_id"], overall_result)
    current_task = _workflow_notification_status_text(status, overall_result)
    phase_text = str(status.get("phase", "") or "unknown").strip()
    mode_text = str(config.get("mode", "manual")).strip()
    completed_count = progress.get("completed_task_count", 0)
    total_count = progress.get("total_task_count", 0)
    pending_count = progress.get("pending_task_count", 0)
    if overall_result == "通过" and phase_text == "done" and total_count:
        completed_count = total_count
        pending_count = 0

    if template == "html":
        lines = [
            "<div style=\"font-size:14px;line-height:1.7;\">",
            "<h2 style=\"margin:0 0 8px 0;font-size:16px;\">任务摘要</h2>",
            "<ul style=\"margin:0 0 16px 18px;padding:0;\">",
            f"<li>会话：<code>{session_meta['session_id']}</code></li>",
            f"<li>仓库：{target_text}</li>",
            f"<li>模式：<code>{mode_text}</code></li>",
            f"<li>耗时：{format_duration(duration_seconds)}</li>",
            f"<li>进度：{completed_count}/{total_count} 已完成，剩余 {pending_count}</li>",
            "</ul>",
            "<h2 style=\"margin:0 0 8px 0;font-size:16px;\">任务结果</h2>",
            "<ul style=\"margin:0 0 0 18px;padding:0;\">",
            f"<li>当前阶段：<code>{phase_text}</code></li>",
            f"<li>当前说明：{current_task}</li>",
            f"<li>下一步：{next_action}</li>",
            "</ul>",
            "</div>",
        ]
        return title, "".join(lines)

    if template == "txt":
        lines = [
            "【任务摘要】",
            f"会话：{session_meta['session_id']}",
            f"仓库：{target_text}",
            f"模式：{mode_text}｜耗时：{format_duration(duration_seconds)}",
            f"进度：{completed_count}/{total_count} 已完成，剩余 {pending_count}",
            "",
            "【任务结果】",
            f"阶段：{phase_text}",
            f"说明：{current_task or '暂无'}",
        ]
        return title, "\n".join(lines)

    lines = [
        "## 任务摘要",
        "",
        f"会话：`{session_meta['session_id']}`  ",
        f"仓库：{target_text}  ",
        f"模式：`{mode_text}`｜耗时：{format_duration(duration_seconds)}  ",
        f"进度：{completed_count}/{total_count} 已完成，剩余 {pending_count}",
        "",
        "## 任务结果",
        "",
        f"阶段：`{phase_text}`  ",
        f"说明：{current_task or '暂无'}",
    ]
    return title, "\n".join(lines)


def build_feishu_notification_payload(
    config: dict[str, Any],
    session_meta: dict[str, Any],
    plan: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
    title: str,
) -> dict[str, Any]:
    duration_seconds = workflow_duration_seconds(session_meta, status, status.get("finished_at", ""))
    progress = plan.get("todo_progress", {})
    targets = [str(item.get("name", "")).strip() for item in plan.get("target_codebases", []) if str(item.get("name", "")).strip()]
    target_text = "、".join(targets) if targets else "未识别"
    current_task = _workflow_notification_status_text(status, overall_result)
    phase_text = str(status.get("phase", "") or "unknown").strip()
    mode_text = str(config.get("mode", "manual")).strip()
    completed_count = progress.get("completed_task_count", 0)
    total_count = progress.get("total_task_count", 0)
    pending_count = progress.get("pending_task_count", 0)
    if overall_result == "通过" and phase_text == "done" and total_count:
        completed_count = total_count
        pending_count = 0
    status_emoji = "✅" if overall_result == "通过" else "❌"
    header_template = "green" if overall_result == "通过" else "red"
    reports = {
        "plan.md": workspace_relative_path(config, report_artifact_path(config, "plan.md", session_meta)),
        "review.md": workspace_relative_path(config, report_artifact_path(config, "review.md", session_meta)),
        "verify.md": workspace_relative_path(config, report_artifact_path(config, "verify.md", session_meta)),
    }
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "style": {
                    "text_size": {
                        "normal_v2": {
                            "default": "normal",
                            "pc": "normal",
                            "mobile": "heading",
                        }
                    }
                },
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": title,
                },
                "template": header_template,
                "padding": "12px 12px 12px 12px",
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            "**任务摘要**\n"
                            f"- 会话：`{session_meta['session_id']}`\n"
                            f"- 仓库：{target_text}\n"
                            f"- 模式：`{mode_text}`\n"
                            f"- 耗时：{format_duration(duration_seconds)}\n"
                            f"- 进度：{completed_count}/{total_count} 已完成，剩余 {pending_count}"
                        ),
                        "text_align": "left",
                        "text_size": "normal_v2",
                    },
                    {
                        "tag": "markdown",
                        "content": (
                            "**任务结果**\n"
                            f"- 阶段：`{phase_text}`\n"
                            f"- 说明：{current_task or '暂无'}\n"
                            f"- 通知来源：`speclane verify`\n"
                            f"- 报告：`plan.md` / `review.md` / `verify.md`"
                        ),
                        "text_align": "left",
                        "text_size": "normal_v2",
                    },
                    {
                        "tag": "markdown",
                        "content": (
                            "**报告路径**\n"
                            f"- plan：`{reports['plan.md']}`\n"
                            f"- review：`{reports['review.md']}`\n"
                            f"- verify：`{reports['verify.md']}`"
                        ),
                        "text_align": "left",
                        "text_size": "normal_v2",
                    },
                ],
            },
        },
    }


def send_feishu_notification_python(webhook_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib_request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = getattr(response, "status", 200)
    except urllib_error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        result = _normalize_feishu_response(error.code, body, "python")
        result["message"] = str(result.get("message") or f"飞书 HTTP {error.code}")
        return result
    except urllib_error.URLError as error:
        return {
            "route": "feishu",
            "channel": "webhook",
            "template": "interactive",
            "sender": "python",
            "status": "failed",
            "success": False,
            "message": f"飞书 webhook 请求失败：{error.reason}",
            "response": {},
        }
    return _normalize_feishu_response(status_code, body, "python")


def send_feishu_notification_curl(webhook_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    curl_path = shutil.which("curl")
    if not curl_path:
        return {
            "route": "feishu",
            "channel": "webhook",
            "template": "interactive",
            "sender": "curl",
            "status": "failed",
            "success": False,
            "message": "系统中未找到 curl，无法执行飞书回退发送。",
            "response": {},
        }
    result = subprocess.run(
        [
            curl_path,
            "-sS",
            webhook_url,
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(payload, ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        return {
            "route": "feishu",
            "channel": "webhook",
            "template": "interactive",
            "sender": "curl",
            "status": "failed",
            "success": False,
            "message": f"飞书 curl 回退发送失败：{result.stderr.strip() or result.stdout.strip() or result.returncode}",
            "response": {},
        }
    return _normalize_feishu_response(200, result.stdout, "curl")


def send_feishu_notification(
    feishu: dict[str, Any],
    config: dict[str, Any],
    session_meta: dict[str, Any],
    plan: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
) -> dict[str, Any]:
    title = _workflow_notification_title(session_meta["session_id"], overall_result)
    payload = build_feishu_notification_payload(config, session_meta, plan, status, overall_result, title)
    secret = str(feishu.get("secret", "")).strip()
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_sign(secret, timestamp)
    webhook_url = str(feishu.get("webhook_url", "")).strip()
    python_result = send_feishu_notification_python(webhook_url, payload)
    if python_result.get("success"):
        return python_result
    curl_result = send_feishu_notification_curl(webhook_url, payload)
    if curl_result.get("success"):
        curl_result["message"] = f"{curl_result.get('message', '')}（Python失败后已回退curl成功）"
        curl_result["python_error"] = python_result.get("message", "")
        return curl_result
    curl_result["python_error"] = python_result.get("message", "")
    curl_result["message"] = (
        f"Python发送失败：{python_result.get('message', '')}；"
        f"curl回退也失败：{curl_result.get('message', '')}"
    )
    return curl_result


def notify_workflow_result(
    config: dict[str, Any],
    session_meta: dict[str, Any],
    plan: dict[str, Any],
    status: dict[str, Any],
    overall_result: str,
) -> dict[str, Any]:
    notification_path = data_artifact_path(config, "notification.json", session_meta)
    existing_result = read_json(notification_path, {})
    current_fingerprint = workflow_notification_fingerprint(session_meta, status, overall_result)
    if (
        isinstance(existing_result, dict)
        and is_standard_workflow_notification(config, session_meta, status, overall_result, existing_result)
    ):
        deduped_result = dict(existing_result)
        deduped_result["deduplicated"] = True
        deduped_result["message"] = "通知已发送，已跳过重复发送。"
        return deduped_result

    pushplus = pushplus_config(config)
    feishu = feishu_config(config)
    enabled_routes = [item for item in pushplus.get("routes", []) if item.get("enabled")]
    if feishu.get("enabled"):
        enabled_routes.append({"name": "feishu", "channel": "webhook", "template": "interactive"})
    result: dict[str, Any] = {
        "provider": "notification",
        "source": "run-workflow.py verify",
        "schema_version": 1,
        "fingerprint": current_fingerprint,
        "enabled": bool(enabled_routes),
        "status": "skipped",
        "message": "未配置通知。",
        "sent_at": now_iso(),
        "title": "",
        "routes": enabled_routes,
        "results": [],
    }
    if enabled_routes:
        route_results = []
        last_title = ""
        for route in enabled_routes:
            if route.get("name") == "feishu":
                last_title = _workflow_notification_title(session_meta["session_id"], overall_result)
                route_results.append(
                    send_feishu_notification(feishu, config, session_meta, plan, status, overall_result)
                )
                continue
            title, content = build_workflow_notification(
                config,
                session_meta,
                plan,
                status,
                overall_result,
                template=str(route.get("template", "markdown") or "markdown"),
            )
            last_title = title
            route_results.append(send_pushplus_notification(pushplus, route, title, content))
        sent_count = sum(1 for item in route_results if item.get("status") == "sent")
        if sent_count == len(route_results):
            result["status"] = "sent"
        elif sent_count > 0:
            result["status"] = "partial"
        else:
            result["status"] = "failed"
        result["title"] = last_title
        result["message"] = "；".join(
            f"{item.get('route', 'unknown')}:{item.get('message', '')}" for item in route_results
        )
        result["results"] = route_results
        result["sent_at"] = now_iso()
    write_managed_json(config, notification_path, result)
    return result


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
