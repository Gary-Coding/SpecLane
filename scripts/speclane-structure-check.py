#!/usr/bin/env python3
from __future__ import annotations

import ast
import py_compile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "skills" / "speclane" / "scripts"
LIB_DIR = SCRIPT_DIR / "lib"
COMMON = SCRIPT_DIR / "common.py"

REQUIRED_LIBS = {
    "artifact_guard.py",
    "io_utils.py",
    "lark.py",
    "lock.py",
    "notify.py",
    "openspec.py",
    "project_detect.py",
    "references.py",
    "session.py",
    "state.py",
    "time_utils.py",
    "todo.py",
    "workspace.py",
    "yaml_utils.py",
}

LOW_LEVEL = {"io_utils", "time_utils", "yaml_utils"}
RUNTIME = {"artifact_guard", "lark", "lock", "session", "workspace"}
DOMAIN = {"notify", "openspec", "project_detect", "references", "state", "todo"}
KNOWN_MODULES = LOW_LEVEL | RUNTIME | DOMAIN


def fail(message: str) -> None:
    raise SystemExit(f"structure_check=failed\n{message}")


def module_name(path: Path) -> str:
    return path.stem


def relative_lib_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "common" or alias.name.startswith("common."):
                    imports.add("common")
    return imports


def check_required_modules() -> None:
    existing = {path.name for path in LIB_DIR.glob("*.py")}
    missing = sorted(REQUIRED_LIBS - existing)
    if missing:
        fail("缺少必需 lib 模块：" + ", ".join(missing))


def check_common_size() -> None:
    line_count = len(COMMON.read_text(encoding="utf-8").splitlines())
    if line_count > 260:
        fail(f"common.py 行数过大：{line_count}，请继续拆分到 lib/*。")


def check_no_reverse_common_import() -> None:
    offenders: list[str] = []
    for path in sorted(LIB_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "import common" in text or "from common import" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    if offenders:
        fail("lib 模块禁止依赖 common.py：" + ", ".join(offenders))


def check_dependency_direction() -> None:
    violations: list[str] = []
    graph: dict[str, set[str]] = {}
    for path in sorted(LIB_DIR.glob("*.py")):
        name = module_name(path)
        if name not in KNOWN_MODULES:
            continue
        imports = relative_lib_imports(path)
        graph[name] = imports & KNOWN_MODULES
        if "common" in imports:
            violations.append(f"{name} -> common")
        if name in LOW_LEVEL:
            forbidden = sorted(imports & (RUNTIME | DOMAIN))
            violations.extend(f"{name} -> {item}" for item in forbidden)
    violations.extend(_dependency_cycles(graph))
    if violations:
        fail("模块依赖方向不合法：" + ", ".join(violations))


def _dependency_cycles(graph: dict[str, set[str]]) -> list[str]:
    cycles: list[str] = []
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            cycle = visiting[visiting.index(node):] + [node]
            cycles.append(" -> ".join(cycle))
            return
        if node in visited:
            return
        visiting.append(node)
        for child in sorted(graph.get(node, set())):
            visit(child)
        visiting.pop()
        visited.add(node)

    for node in sorted(graph):
        visit(node)
    return cycles


def check_compile() -> None:
    for path in [*sorted(SCRIPT_DIR.glob("*.py")), *sorted(LIB_DIR.glob("*.py"))]:
        py_compile.compile(str(path), doraise=True)


def main() -> None:
    if not COMMON.exists():
        fail("缺少 common.py 兼容导出层。")
    check_required_modules()
    check_common_size()
    check_no_reverse_common_import()
    check_dependency_direction()
    check_compile()
    print("structure_check=ok")


if __name__ == "__main__":
    main()
