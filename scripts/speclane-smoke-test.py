#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "speclane.js"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sl-smoke-") as tmp:
        root = Path(tmp)
        code_dir = root / "code"
        workspace = root / "ai-workspace"
        code_dir.mkdir()

        run(
            [
                "node",
                str(CLI),
                "init",
                "--yes",
                "--install",
                "none",
                "--workspace",
                str(workspace),
                "--code-path",
                "../code",
                "--demand-name",
                "1-demo",
                "--source",
                "openspec",
                "--mode",
                "auto",
            ]
        )
        run(["node", str(CLI), "doctor", "--workspace", str(workspace)])
        run(["node", str(CLI), "migrate", "--workspace", str(workspace), "--dry-run"])
        assert_file(workspace / "workspace.yml")
        assert_file(workspace / "demands" / "1-demo" / "需求.md")
        assert_file(workspace / ".speclane" / "active-demand.yml")
        assert_file(workspace / ".speclane" / "demands" / "1-demo" / "demand.yml")
        assert_dir(workspace / "openspec" / "changes")
        assert_file(workspace / ".claude" / "commands" / "sl" / "propose.md")
        assert_file(workspace / ".claude" / "commands" / "sl" / "bridge.md")
        workspace_yml = (workspace / "workspace.yml").read_text(encoding="utf-8")
        demand_yml = (workspace / ".speclane" / "demands" / "1-demo" / "demand.yml").read_text(encoding="utf-8")
        if "demand_defaults:" not in workspace_yml:
            raise AssertionError("workspace.yml should contain demand_defaults for multi-demand standard layout")
        if "demand_name: 1-demo" not in demand_yml or "workflow_source: openspec" not in demand_yml:
            raise AssertionError("initial demand.yml did not contain expected demand instance config")

        legacy = root / "legacy-workspace"
        legacy.mkdir()
        (legacy / "workspace.yml").write_text("todo_file: todo.md\n", encoding="utf-8")
        run(["node", str(CLI), "migrate", "--workspace", str(legacy)])
        migrated = (legacy / "workspace.yml").read_text(encoding="utf-8")
        if "workflow_source: todo" not in migrated or "reference_files:" not in migrated:
            raise AssertionError("legacy workspace migration did not add required fields")

    print("smoke_test=ok")


def run(command: list[str]) -> None:
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout)
        raise SystemExit(result.returncode)


def assert_file(path: Path) -> None:
    if not path.is_file():
        raise AssertionError(f"missing file: {path}")


def assert_dir(path: Path) -> None:
    if not path.is_dir():
        raise AssertionError(f"missing directory: {path}")


if __name__ == "__main__":
    if shutil.which("node") is None:
        raise SystemExit("node is required")
    main()
