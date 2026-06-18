from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .io_utils import read_json
from .lark import is_lark_doc_url, is_url
from .session import active_openspec_change_path
from .time_utils import now_iso
from .yaml_utils import parse_simple_yaml


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
    return Path(__file__).resolve().parents[2]


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


