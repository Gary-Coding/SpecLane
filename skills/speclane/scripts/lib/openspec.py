from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .io_utils import compact_text_excerpt, file_sha256, read_json, read_text, unique, write_json
from .project_detect import code_root, find_candidate_codebases, looks_like_project_root
from .session import active_openspec_change_path, artifacts_dir, workflow_source
from .time_utils import now_iso


def normalize_todo_text_item(text: str) -> str:
    normalized = text.strip()
    normalized = re.sub(r"^\d+\.\s*", "", normalized)
    normalized = re.sub(r"^[-*]\s*", "", normalized)
    normalized = re.sub(r"^\[(?: |x|X)\]\s*", "", normalized)
    return normalized.strip()


def _extract_service_hints_from_lines(lines: list[str]) -> list[str]:
    hints: list[str] = []
    patterns = [
        r"(?:修改的服务|目标服务|服务|service|repo|repository|module|模块)\s*(?:是|为|:|：)\s*`?([A-Za-z0-9_.-]+)`?",
        r"`([A-Za-z0-9_.-]+)`",
    ]
    stopwords = {"todo", "tasks", "service", "module", "repo", "repository", "true", "false"}
    for line in lines:
        for pattern in patterns:
            for match in re.finditer(pattern, line, flags=re.IGNORECASE):
                value = match.group(1).strip("`'\"，。、；：:()（）[]【】")
                if len(value) >= 2 and value.lower() not in stopwords:
                    hints.append(value)
    return unique(hints)


def write_managed_json(config: dict[str, Any], path: Path, payload: Any) -> None:
    write_json(path, payload)

def openspec_tasks_hash(config: dict[str, Any]) -> str:
    if workflow_source(config) != "openspec":
        return ""
    return file_sha256(openspec_tasks_path(config))


def is_openspec_placeholder_text(text: str) -> bool:
    normalized = str(text).strip()
    if not normalized:
        return True
    lowered = normalized.lower()
    markers = [
        "待填写",
        "待补充",
        "当前 blocked",
        "当前blocked",
        "blocked",
        "placeholder",
        "todo",
        "tbd",
        "依赖 proposal",
        "请在这里",
        "在这里写",
    ]
    return any(marker in lowered for marker in markers)


def validate_openspec_change_artifacts(config: dict[str, Any]) -> dict[str, Any]:
    if workflow_source(config) != "openspec":
        return {"valid": True, "errors": [], "warnings": []}
    change_dir = openspec_change_dir(config)
    proposal = change_dir / "proposal.md"
    design = change_dir / "design.md"
    tasks = openspec_tasks_path(config)
    specs_dir = change_dir / "specs"
    errors: list[str] = []
    warnings: list[str] = []

    required_files = {
        "proposal.md": proposal,
        "design.md": design,
        "tasks.md": tasks,
    }
    for label, path in required_files.items():
        if not path.exists() or not path.is_file():
            errors.append(f"缺少 OpenSpec 产物：{label}")
            continue
        text = read_text(path)
        if is_openspec_placeholder_text(text):
            errors.append(f"{label} 仍是占位或未完成内容")
        if label == "tasks.md" and not re.search(r"(?m)^\s*-\s+\[[ xX]\]\s+\S+", text):
            errors.append("tasks.md 缺少可执行任务项")

    spec_files = sorted(specs_dir.rglob("*.md")) if specs_dir.exists() and specs_dir.is_dir() else []
    valid_spec_files = [
        path for path in spec_files
        if path.is_file() and not is_openspec_placeholder_text(read_text(path))
    ]
    design_text = read_text(design)
    no_spec_required = any(marker in design_text for marker in ("无需新增规格", "无需新增 specs", "无需新增 spec", "no new specs"))
    if not valid_spec_files and not no_spec_required:
        errors.append("缺少有效 specs/*.md；如确实不需要新增规格，请在 design.md 明确写明“无需新增规格”")
    elif not valid_spec_files:
        warnings.append("design.md 声明无需新增规格，跳过 specs 文件要求")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "change_dir": str(change_dir),
        "proposal": str(proposal),
        "design": str(design),
        "tasks": str(tasks),
        "spec_count": len(valid_spec_files),
    }


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


