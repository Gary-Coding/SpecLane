#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "skills" / "speclane" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from common import (  # noqa: E402
    active_session_for_plan,
    configured_demand_name,
    create_session,
    current_session_file,
    data_artifact_path,
    expand_workspace_variables,
    load_workspace_config,
    normalize_verify_commands,
    read_json,
    resolve_requested_demand,
    validate_demand_name,
    write_json,
    write_managed_json,
)
from lib.io_utils import compact_text_excerpt, relative_to, unique  # noqa: E402
from lib.time_utils import format_duration, parse_iso_datetime  # noqa: E402
from lib.yaml_utils import parse_simple_yaml  # noqa: E402


def main() -> None:
    test_yaml_parser()
    test_workspace_variables_and_demand_selection()
    test_workspace_config_and_session_reuse()
    test_io_and_time_helpers()
    print("unit_test=ok")


def test_yaml_parser() -> None:
    data = parse_simple_yaml(
        """
version: 1
enabled: true
count: 3
items:
  - name: one
    desc: 第一个
  - name: two
refs: [a.md, "b.md"]
empty: []
"""
    )
    assert data["version"] == 1
    assert data["enabled"] is True
    assert data["count"] == 3
    assert data["items"][0]["name"] == "one"
    assert data["items"][0]["desc"] == "第一个"
    assert data["items"][1]["name"] == "two"
    assert data["refs"] == ["a.md", "b.md"]
    assert data["empty"] == []


def test_workspace_variables_and_demand_selection() -> None:
    with tempfile.TemporaryDirectory(prefix="sl-unit-demand-") as tmp:
        root = Path(tmp)
        raw = {
            "vars": {"base": "demands/${vars.demand_name}"},
            "demands": [
                {"name": "1-alpha", "desc": "alpha"},
                {"name": "2-beta", "desc": "beta"},
            ],
        }
        assert resolve_requested_demand(root, raw, "1-alpha") == "1-alpha"
        try:
            resolve_requested_demand(root, raw, "missing")
        except ValueError as exc:
            assert "不存在需求配置" in str(exc)
        else:
            raise AssertionError("unknown demand should fail")

        expanded = expand_workspace_variables(
            {"vars": {"demand_name": "1-alpha"}, "path": "demands/${demand_name}/todo.md"},
            root,
        )
        assert expanded["path"] == "demands/1-alpha/todo.md"
        assert configured_demand_name(expanded) == "1-alpha"

        assert validate_demand_name("abc-1") == "abc-1"
        for invalid in ("", "../x", "中文", "-bad"):
            try:
                validate_demand_name(invalid)
            except ValueError:
                pass
            else:
                raise AssertionError(f"invalid demand name accepted: {invalid}")


def test_workspace_config_and_session_reuse() -> None:
    with tempfile.TemporaryDirectory(prefix="sl-unit-workspace-") as tmp:
        root = Path(tmp)
        home = root / "home"
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        workspace = root / "workspace"
        code = root / "code"
        demand_dir = workspace / "demands" / "1-demo"
        code.mkdir(parents=True)
        (code / "package.json").write_text(json.dumps({"scripts": {"test": "node -e 1"}}), encoding="utf-8")
        (demand_dir / "input").mkdir(parents=True)
        (demand_dir / "input" / "需求.md").write_text("# 需求\n", encoding="utf-8")
        (workspace / "workspace.yml").write_text(
            "\n".join(
                [
                    "version: 1",
                    "workflow_source: todo",
                    "mode: auto",
                    "code_path: ../code",
                    "demands:",
                    "  - name: 1-demo",
                    "    desc: demo",
                    "    demand_file: demands/${demand_name}/input/需求.md",
                    "    todo_file: demands/${demand_name}/spec/bridge/todo.md",
                    "    output_dir: demands/${demand_name}/rd/output",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        config = load_workspace_config(workspace)
        assert config["__demand_name"] == "1-demo"
        assert config["mode"] == "auto"
        assert config["workflow_source"] == "todo"
        assert config["demand_file"].endswith("demands/1-demo/input/需求.md")
        assert normalize_verify_commands("npm test") == {"default": "npm test"}

        session = create_session(config)
        assert current_session_file(config).exists()
        active = active_session_for_plan(config)
        assert active and active["session"]["session_id"] == session["session_id"]
        write_managed_json(config, data_artifact_path(config, "plan.json", session), {"ok": True})
        active_with_plan = active_session_for_plan(config)
        assert active_with_plan and active_with_plan["incomplete"] is False
        write_json(data_artifact_path(config, "status.json", session), {"phase": "done"})
        assert active_session_for_plan(config) is None


def test_io_and_time_helpers() -> None:
    text = "\n".join([f"line {index}" for index in range(120)]) + "\nimportant keyword\n"
    excerpt = compact_text_excerpt(text, keywords=["keyword"], max_chars=200)
    assert "important keyword" in excerpt
    assert "已摘要" in excerpt
    assert unique(["a", "b", "a", ""]) == ["a", "b"]
    assert relative_to(Path("/tmp/a/b"), Path("/tmp")) == "a/b"
    assert parse_iso_datetime("2026-06-18T00:00:00Z") is not None
    assert format_duration(61) == "1 分1 秒"
    assert read_json(Path("/not/exist"), {"fallback": True}) == {"fallback": True}


if __name__ == "__main__":
    main()
