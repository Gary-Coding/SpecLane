#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"
RD_SKILL_DIR = SKILLS_ROOT / "speclane-rd"
PACKAGE_JSON = REPO_ROOT / "package.json"
TEMPLATE_DIR = REPO_ROOT / "templates" / "workspaces"


WORKSPACE_TEMPLATES: dict[str, str] = {
    "openspec-auto": "OpenSpec 规格先行 + 自动交付，推荐用于需求迭代主流程。",
    "openspec-manual": "OpenSpec 规格先行 + 分阶段人工确认，推荐用于高风险变更。",
    "todo-auto": "todo.md 直接驱动 + 自动交付，推荐用于小需求或已有明确任务清单。",
    "todo-manual": "todo.md 直接驱动 + 分阶段人工确认，推荐用于首次接入或新手练习。",
    "java-microservice": "Java / Spring 微服务模板，内置 Maven 验证命令示例。",
    "frontend": "Vue / React 前端模板，内置 pnpm/npm 验证命令示例。",
    "multi-repo": "多仓库聚合目录模板，适用于中台或跨服务需求。",
}


SL_COMMANDS: dict[str, str] = {
    "propose.md": """---
description: SpecLane：生成或完善 OpenSpec change
argument-hint: <change-name>
---

请使用 SpecLane RD skill 执行：`/sl:propose $ARGUMENTS`。
如果 `$ARGUMENTS` 为空，请先询问用户提供 OpenSpec change 名称。
""",
    "propose-fix.md": """---
description: SpecLane：需求补充后修正当前 OpenSpec change
argument-hint: <change-name>
---

请使用 SpecLane RD skill 执行：`/sl:propose $ARGUMENTS`。
当前需求有补充，请修正当前 OpenSpec change；不要创建新的 change，不要改代码。
""",
    "bridge.md": """---
description: SpecLane：生成桥接 todo
---

请使用 SpecLane RD skill 执行：`/sl:bridge`。
生成桥接 todo 并总结待审核项，不要改代码，不要进入实现。
""",
    "plan.md": """---
description: SpecLane：只生成实施计划
---

请使用 SpecLane RD skill 执行：`/sl:plan`。
只生成计划，不要改代码。
""",
    "apply.md": """---
description: SpecLane：审核 todo 后进入交付阶段
---

请使用 SpecLane RD skill 执行：`/sl:apply`。
我已审核当前桥接 todo，可以进入交付阶段。
""",
    "archive-check.md": """---
description: SpecLane：检查 OpenSpec 归档条件
---

请使用 SpecLane RD skill 执行：`/sl:archive-check`。
""",
    "archive.md": """---
description: SpecLane：归档 OpenSpec change
---

请使用 SpecLane RD skill 执行：`/sl:archive`。
""",
    "status.md": """---
description: SpecLane：查看工作流状态
---

请使用 SpecLane RD skill 执行：`/sl:status`。
""",
    "recover.md": """---
description: SpecLane：从标准产物恢复工作流状态
---

请使用 SpecLane RD skill 执行：`/sl:recover`。
只恢复和诊断状态，不要改代码。
""",
    "demand.md": """---
description: SpecLane：管理多需求实例
argument-hint: new|use|list|status <demand-name>
---

请使用 SpecLane RD skill 执行：`/sl:demand $ARGUMENTS`。
只管理需求实例和当前 active demand，不要改业务代码。
""",
}


def main() -> None:
    if len(sys.argv) == 1:
        run_setup([])
        return
    if sys.argv[1] in ("init", "setup"):
        run_setup(sys.argv[2:])
        return

    parser = argparse.ArgumentParser(
        prog="speclane",
        description="SpecLane workflow CLI.",
    )
    parser.add_argument("--version", action="store_true", help="显示版本号。")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="交互式安装 skill 并初始化工作区。")
    subparsers.add_parser("setup", help="init 的别名。")

    install_parser = subparsers.add_parser("install", help="安装 skill 到 Codex / Claude。")
    install_parser.add_argument("--target", choices=["codex", "claude", "both"], default="both")
    install_parser.add_argument("--force", action="store_true", help="安装前删除旧 skill 目录。")

    sync_parser = subparsers.add_parser("sync", help="重新同步 skill 到 Codex / Claude。")
    sync_parser.add_argument("--target", choices=["codex", "claude", "both"], default="both")

    doctor_parser = subparsers.add_parser("doctor", help="检查本机环境和工作区配置。")
    doctor_parser.add_argument("--workspace", default=".", help="工作区目录，默认当前目录。")
    doctor_parser.add_argument("--json", action="store_true", help="输出 JSON。")
    doctor_parser.add_argument("--fix", action="store_true", help="尽量自动补齐 skill 和快捷命令。")

    migrate_parser = subparsers.add_parser("migrate", help="补齐旧工作区缺失的 workspace.yml 配置项。")
    migrate_parser.add_argument("--workspace", default=".", help="工作区目录，默认当前目录。")
    migrate_parser.add_argument("--dry-run", action="store_true", help="只展示计划，不写入文件。")

    subparsers.add_parser("templates", help="列出内置 workspace.yml 模板。")

    commands_parser = subparsers.add_parser("commands", help="安装 AI 编码工具快捷命令模板。")
    commands_subparsers = commands_parser.add_subparsers(dest="commands_action")
    commands_install = commands_subparsers.add_parser("install", help="安装 /sl:* 快捷命令模板。")
    commands_install.add_argument("--workspace", default=".", help="工作区目录，默认当前目录。")
    commands_install.add_argument(
        "--target",
        choices=["claude", "codex", "cursor", "trae", "kimi", "all"],
        default="claude",
        help="目标 AI 编码工具。",
    )

    template_parser = subparsers.add_parser("template", help="查看或复制内置 workspace.yml 模板。")
    template_subparsers = template_parser.add_subparsers(dest="template_action")
    template_show = template_subparsers.add_parser("show", help="打印指定模板内容。")
    template_show.add_argument("name", choices=sorted(WORKSPACE_TEMPLATES))
    template_copy = template_subparsers.add_parser("copy", help="复制指定模板到工作区 workspace.yml。")
    template_copy.add_argument("name", choices=sorted(WORKSPACE_TEMPLATES))
    template_copy.add_argument("--workspace", default=".", help="工作区目录，默认当前目录。")
    template_copy.add_argument("--demand-name", default="1-your-demand", help="写入 vars.demand_name 的需求目录名。")
    template_copy.add_argument("--code-path", default="../code", help="写入 code_path 的代码目录。")
    template_copy.add_argument("--force", action="store_true", help="允许覆盖已有 workspace.yml。")

    subparsers.add_parser("version", help="显示版本号。")

    args = parser.parse_args()
    if args.version or args.command == "version":
        print(package_version())
        return
    if args.command == "install":
        install_targets(args.target, force=args.force)
        return
    if args.command == "sync":
        install_targets(args.target, force=True)
        return
    if args.command == "doctor":
        exit_code = doctor(Path(args.workspace).expanduser().resolve(), output_json=args.json, fix=args.fix)
        raise SystemExit(exit_code)
    if args.command == "migrate":
        exit_code = migrate(Path(args.workspace).expanduser().resolve(), dry_run=args.dry_run)
        raise SystemExit(exit_code)
    if args.command == "templates":
        list_templates()
        return
    if args.command == "commands":
        if args.commands_action == "install":
            install_commands(Path(args.workspace).expanduser().resolve(), args.target)
            return
        commands_parser.print_help()
        return
    if args.command == "template":
        if args.template_action == "show":
            show_template(args.name)
            return
        if args.template_action == "copy":
            copy_template(
                args.name,
                Path(args.workspace).expanduser().resolve(),
                demand_name=args.demand_name,
                code_path=args.code_path,
                force=args.force,
            )
            return
        template_parser.print_help()
        return

    parser.print_help()


def package_version() -> str:
    if not PACKAGE_JSON.exists():
        return "0.0.0"
    data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    return str(data.get("version", "0.0.0"))


def run_setup(args: list[str]) -> None:
    script = REPO_ROOT / "scripts" / "speclane-setup.py"
    result = subprocess.run([sys.executable, str(script), *args], check=False)
    raise SystemExit(result.returncode)


def install_targets(target: str, force: bool) -> None:
    targets = []
    if target in ("codex", "both"):
        targets.append(skills_base("codex"))
    if target in ("claude", "both"):
        targets.append(skills_base("claude"))
    for item in targets:
        install_skill_set(item, force=force)


def skills_base(kind: str) -> Path:
    if kind == "codex":
        base = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    elif kind == "claude":
        base = Path(os.environ.get("CLAUDE_HOME", "~/.claude")).expanduser()
    else:
        raise ValueError(kind)
    return base / "skills"


def install_skill_set(skills_root: Path, force: bool) -> None:
    copy_skill_dir(RD_SKILL_DIR, skills_root / "speclane-rd", force=force)


def copy_skill_dir(source: Path, target: Path, force: bool) -> None:
    if not source.exists():
        raise SystemExit(f"skill 目录不存在：{source}")
    if target.exists() and force:
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    print(f"✓ 已同步 skill: {target}")


def template_path(name: str) -> Path:
    path = TEMPLATE_DIR / f"{name}.yml"
    if not path.exists():
        raise SystemExit(f"模板不存在：{name}")
    return path


def list_templates() -> None:
    print("内置 workspace.yml 模板：")
    for name in sorted(WORKSPACE_TEMPLATES):
        marker = "✓" if template_path(name).exists() else "!"
        print(f"{marker} {name}: {WORKSPACE_TEMPLATES[name]}")


def render_template(name: str, demand_name: str, code_path: str) -> str:
    text = template_path(name).read_text(encoding="utf-8")
    return text.replace("__DEMAND_NAME__", demand_name).replace("__CODE_PATH__", code_path)


def show_template(name: str) -> None:
    print(render_template(name, demand_name="1-your-demand", code_path="../code"))


def copy_template(name: str, workspace: Path, demand_name: str, code_path: str, force: bool) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "workspace.yml"
    if target.exists() and not force:
        raise SystemExit(f"workspace.yml 已存在：{target}。如需覆盖请加 --force。")
    target.write_text(render_template(name, demand_name=demand_name, code_path=code_path), encoding="utf-8")
    (workspace / "docs").mkdir(parents=True, exist_ok=True)
    (workspace / "demands" / demand_name).mkdir(parents=True, exist_ok=True)
    print(f"✓ 已写入模板：{target}")


def doctor(workspace: Path, output_json: bool, fix: bool = False) -> int:
    if fix:
        install_targets("both", force=True)
        install_commands(workspace, "all")

    checks: list[dict[str, str]] = []
    add_check(checks, "platform", "ok", f"{platform.system()} {platform.release()}")
    add_check(checks, "python", "ok", sys.version.split()[0])
    add_check(checks, "node", "ok" if shutil.which("node") else "fail", shutil.which("node") or "未安装")
    add_check(checks, "npm", "ok" if shutil.which("npm") else "fail", shutil.which("npm") or "未安装")
    add_check(checks, "skill_source", "ok" if RD_SKILL_DIR.exists() else "fail", str(RD_SKILL_DIR))
    add_install_checks(checks, "codex", skills_base("codex"))
    add_install_checks(checks, "claude", skills_base("claude"))
    add_check(checks, "openspec_cli", "ok" if shutil.which("openspec") else "warn", shutil.which("openspec") or "未安装")
    add_check(checks, "workspace", "ok" if workspace.exists() else "fail", str(workspace))
    add_check(checks, "workspace.commands.sl", "ok" if workspace_commands_ready(workspace) else "warn", str(workspace / ".claude" / "commands" / "sl"))
    for platform_name in ("codex", "cursor", "trae", "kimi"):
        target_dir = command_target_dirs(workspace, platform_name)[0][1]
        add_check(
            checks,
            f"workspace.commands.{platform_name}",
            "ok" if commands_ready_for_target(workspace, platform_name) else "warn",
            str(target_dir),
        )
    add_check(checks, "workspace.openspec.root", "ok" if (workspace / "openspec").exists() else "warn", str(workspace / "openspec"))

    workspace_yml = workspace / "workspace.yml"
    add_check(checks, "workspace_yml", "ok" if workspace_yml.exists() else "fail", str(workspace_yml))
    if workspace_yml.exists():
        try:
            config = read_workspace_yaml(workspace_yml)
        except ValueError as exc:
            add_check(checks, "workspace_yml.parse", "fail", str(exc))
        else:
            add_check(checks, "workspace_yml.parse", "ok", "解析成功")
            validate_workspace(checks, workspace, config)

    if output_json:
        print(json.dumps({"checks": checks}, ensure_ascii=False, indent=2))
    else:
        print("SpecLane doctor")
        for check in checks:
            mark = {"ok": "✓", "warn": "!", "fail": "✗"}[check["status"]]
            print(f"{mark} {check['name']}: {check['message']}")
        suggestions = doctor_suggestions(checks)
        if suggestions:
            print("\n建议：")
            for item in suggestions:
                print(f"- {item}")

    return 1 if any(item["status"] == "fail" for item in checks) else 0


def workspace_commands_ready(workspace: Path) -> bool:
    commands_dir = workspace / ".claude" / "commands" / "sl"
    required = ["propose.md", "bridge.md", "plan.md", "apply.md", "status.md", "recover.md", "demand.md"]
    return all((commands_dir / name).exists() for name in required)


def commands_ready_for_target(workspace: Path, target: str) -> bool:
    for name, directory in command_target_dirs(workspace, target):
        required = [f"sl-{filename}" if name == "codex" else filename for filename in SL_COMMANDS]
        if not all((directory / filename).exists() for filename in required):
            return False
    return True


def add_install_checks(checks: list[dict[str, str]], name: str, base: Path) -> None:
    required = [
        base / "speclane-rd" / "SKILL.md",
        base / "speclane-rd" / "scripts" / "run-workflow.py",
    ]
    status = "ok" if all(path.exists() for path in required) else "warn"
    add_check(checks, f"{name}_skills", status, str(base))


def ensure_workspace_commands(workspace: Path) -> None:
    install_commands(workspace, "claude")


def command_target_dirs(workspace: Path, target: str) -> list[tuple[str, Path]]:
    home = Path.home()
    mapping: dict[str, Path] = {
        "claude": workspace / ".claude" / "commands" / "sl",
        "codex": Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser() / "prompts",
        "cursor": workspace / ".cursor" / "commands" / "sl",
        "trae": workspace / ".trae" / "commands" / "sl",
        "kimi": workspace / ".kimi" / "commands" / "sl",
    }
    if target == "all":
        return list(mapping.items())
    return [(target, mapping[target])]


def install_commands(workspace: Path, target: str) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for name, directory in command_target_dirs(workspace, target):
        directory.mkdir(parents=True, exist_ok=True)
        for filename, content in SL_COMMANDS.items():
            output_name = f"sl-{filename}" if name == "codex" else filename
            (directory / output_name).write_text(content, encoding="utf-8")
        print(f"✓ 已补齐 {name} 快捷命令: {directory}")


def doctor_suggestions(checks: list[dict[str, str]]) -> list[str]:
    by_name = {item["name"]: item for item in checks}
    suggestions: list[str] = []
    if by_name.get("codex_skills", {}).get("status") != "ok" or by_name.get("claude_skills", {}).get("status") != "ok":
        suggestions.append("执行 `speclane sync --target both` 同步最新 skill。")
    if by_name.get("workspace.commands.sl", {}).get("status") != "ok":
        suggestions.append("执行 `speclane doctor --fix` 补齐工作区 `.claude/commands/sl/*` 快捷命令。")
    if by_name.get("workspace_yml", {}).get("status") != "ok":
        suggestions.append("执行 `speclane init` 初始化工作区，或用 `speclane template copy <模板名>` 生成 workspace.yml。")
    if by_name.get("openspec_cli", {}).get("status") != "ok":
        suggestions.append("OpenSpec 模式建议先安装并初始化 OpenSpec；todo 模式可忽略。")
    if by_name.get("workspace.openspec.root", {}).get("status") != "ok":
        suggestions.append("OpenSpec 模式请在工作区执行 OpenSpec 初始化；todo 模式可忽略。")
    if by_name.get("node", {}).get("status") != "ok" or by_name.get("npm", {}).get("status") != "ok":
        suggestions.append("请先安装 Node.js/npm。")
    return suggestions


def add_check(checks: list[dict[str, str]], name: str, status: str, message: str) -> None:
    checks.append({"name": name, "status": status, "message": message})


def read_workspace_yaml(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except Exception:
        yaml = None
    if yaml is not None:
        loaded = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict):
            raise ValueError("workspace.yml 顶层必须是对象")
        return loaded

    common_path = RD_SKILL_DIR / "scripts" / "common.py"
    spec = importlib.util.spec_from_file_location("st_common_for_cli", common_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"无法加载 YAML 解析器：{common_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    loaded = module.parse_simple_yaml(text)
    if not isinstance(loaded, dict):
        raise ValueError("workspace.yml 顶层必须是对象")
    return loaded


def config_get(config: dict[str, object], key: str, default: object = None) -> object:
    current: object = config
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def config_str(config: dict[str, object], key: str, default: str = "") -> str:
    value = config_get(config, key, default)
    if value is None:
        return default
    return str(value)


def validate_workspace(checks: list[dict[str, str]], workspace: Path, config: dict[str, object]) -> None:
    config = merged_workspace_config(workspace, config)
    for key in ("workflow_source", "mode", "code_path", "output_dir"):
        value = config_str(config, key)
        add_check(checks, f"config.{key}", "ok" if value else "fail", value or "缺失")

    version = config_get(config, "version")
    add_check(checks, "config.version", "ok" if version == 1 else "fail", str(version or "缺失"))

    references = config_get(config, "reference_files")
    add_check(checks, "config.reference_files", "ok" if isinstance(references, list) else "fail", "list" if isinstance(references, list) else "缺失或非数组")

    source = config_str(config, "workflow_source")
    if source not in ("openspec", "todo"):
        add_check(checks, "config.workflow_source.value", "fail", source or "缺失")

    mode = config_str(config, "mode")
    if mode not in ("auto", "manual"):
        add_check(checks, "config.mode.value", "fail", mode or "缺失")

    code_path = config_str(config, "code_path")
    if code_path:
        resolved = resolve_workspace_path(workspace, code_path)
        add_check(checks, "code_path.exists", "ok" if resolved.exists() else "warn", str(resolved))

    demand_file = config_str(config, "demand_file")
    if source == "openspec":
        add_check(checks, "config.demand_file", "ok" if demand_file else "fail", demand_file or "缺失")
        if demand_file:
            resolved = resolve_workspace_path(workspace, expand_vars(demand_file, config))
            add_check(checks, "demand_file.exists", "ok" if resolved.exists() else "warn", str(resolved))
        changes_dir = config_str(config, "openspec.changes_dir", "openspec/changes")
        resolved_changes = resolve_workspace_path(workspace, changes_dir)
        add_check(checks, "openspec.changes_dir", "ok" if resolved_changes.exists() else "warn", str(resolved_changes))

    todo_file = config_str(config, "todo_file")
    add_check(checks, "config.todo_file", "ok" if todo_file else "fail", todo_file or "缺失")
    if todo_file:
        resolved = resolve_workspace_path(workspace, expand_vars(todo_file, config))
        status = "ok" if resolved.exists() else ("warn" if source == "openspec" else "fail")
        add_check(checks, "todo_file.exists", status, str(resolved))


def merged_workspace_config(workspace: Path, config: dict[str, object]) -> dict[str, object]:
    demand_name = active_demand_name(workspace) or config_str(config, "vars.demand_name")
    if not demand_name:
        return config
    demand_yml = workspace / ".speclane" / "demands" / demand_name / "demand.yml"
    if not demand_yml.exists():
        return config
    try:
        demand_config = read_workspace_yaml(demand_yml)
    except ValueError:
        return config
    merged = dict(config)
    for key, value in demand_config.items():
        if key == "version":
            continue
        merged[key] = value
    vars_map = dict(merged.get("vars", {}) if isinstance(merged.get("vars", {}), dict) else {})
    vars_map["demand_name"] = demand_name
    merged["vars"] = vars_map
    return merged


def active_demand_name(workspace: Path) -> str:
    path = workspace / ".speclane" / "active-demand.yml"
    if not path.exists():
        return ""
    try:
        data = read_workspace_yaml(path)
    except ValueError:
        return ""
    return str(data.get("demand_name", "")).strip()


def expand_vars(value: str, config: dict[str, object]) -> str:
    demand_name = config_str(config, "vars.demand_name")
    return value.replace("${demand_name}", demand_name)


def resolve_workspace_path(workspace: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (workspace / path).resolve()


def migrate(workspace: Path, dry_run: bool) -> int:
    workspace_yml = workspace / "workspace.yml"
    if not workspace_yml.exists():
        print(f"✗ workspace.yml 不存在：{workspace_yml}")
        return 1

    try:
        config = read_workspace_yaml(workspace_yml)
    except ValueError as exc:
        print(f"✗ workspace.yml 无法解析：{exc}")
        return 1

    additions = default_missing_lines(config)
    if not additions:
        print("✓ workspace.yml 已是当前版本，无需迁移。")
        return 0

    print("将补齐以下配置：")
    for line in additions:
        print(f"  {line}")

    if dry_run:
        return 0

    with workspace_yml.open("a", encoding="utf-8") as handle:
        handle.write("\n# Added by speclane migrate\n")
        for line in additions:
            handle.write(f"{line}\n")
    print(f"✓ 已迁移：{workspace_yml}")
    return 0


def default_missing_lines(config: dict[str, object]) -> list[str]:
    additions: list[str] = []
    if config_get(config, "version") is None:
        additions.append("version: 1")
    if not config_str(config, "mode"):
        additions.append("mode: manual")
    if not config_str(config, "workflow_source"):
        additions.append("workflow_source: todo")
    if config_get(config, "reference_files") is None:
        additions.extend(["reference_files:", "  - docs/项目介绍.md"])
    if not config_str(config, "code_path"):
        additions.append("code_path: ../your-project")
    if not config_str(config, "output_dir"):
        additions.append("output_dir: output")
    return additions


if __name__ == "__main__":
    main()
