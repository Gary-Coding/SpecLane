from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .io_utils import read_text, unique
from .yaml_utils import parse_simple_yaml


def _skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def code_root(config: dict[str, Any]) -> Path:
    return Path(str(config["code_path"])).resolve()


def todo_path(config: dict[str, Any]) -> Path:
    return Path(str(config["todo_file"])).resolve()


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


def _service_hints_from_todo(todo_text: str) -> list[str]:
    hints: list[str] = []
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
    target_lines: list[str] = []
    for title, lines in sections.items():
        if any(keyword in title for keyword in ("限制条件", "约束", "constraints", "constraint")):
            target_lines.extend(lines)
    if not target_lines:
        target_lines = [line.strip() for line in todo_text.splitlines() if line.strip()]
    patterns = [
        r"(?:修改的服务|目标服务|服务|service|repo|repository|module|模块)\s*(?:是|为|:|：)\s*`?([A-Za-z0-9_.-]+)`?",
        r"`([A-Za-z0-9_.-]+)`",
    ]
    stopwords = {"todo", "tasks", "service", "module", "repo", "repository", "true", "false"}
    for line in target_lines:
        for pattern in patterns:
            for match in re.finditer(pattern, line, flags=re.IGNORECASE):
                value = match.group(1).strip("`'\"，。、；：:()（）[]【】")
                if len(value) >= 2 and value.lower() not in stopwords:
                    hints.append(value)
    return unique(hints)

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
    hints = _service_hints_from_todo(todo_text)

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
    adapters_dir = _skill_root() / "adapters"
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
