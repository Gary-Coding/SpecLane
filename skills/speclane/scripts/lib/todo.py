from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .artifact_guard import write_managed_json
from .io_utils import read_text, unique, write_text
from .openspec import (
    build_openspec_bridge_context,
    infer_openspec_service_hints,
    openspec_bridge_context_path,
    openspec_change_dir,
    openspec_change_name,
    openspec_tasks_path,
    transform_openspec_tasks_to_todo,
)
from .project_detect import todo_path
from .session import output_dir, artifacts_dir, workflow_source

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

