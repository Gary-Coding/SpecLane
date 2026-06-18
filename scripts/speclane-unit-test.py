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
    build_feishu_notification_payload,
    configured_demand_name,
    create_session,
    current_session_file,
    data_artifact_path,
    detect_project,
    expand_workspace_variables,
    feishu_sign,
    load_workspace_config,
    normalize_verify_commands,
    openspec_hash_drift,
    validate_openspec_change_artifacts,
    read_json,
    resolve_requested_demand,
    resolve_target_codebases,
    validate_demand_name,
    validate_sl_state,
    write_json,
    write_managed_json,
    workflow_notification_fingerprint,
)
from lib.io_utils import compact_text_excerpt, relative_to, unique  # noqa: E402
from lib.time_utils import format_duration, parse_iso_datetime  # noqa: E402
from lib.yaml_utils import parse_simple_yaml  # noqa: E402


def main() -> None:
    test_yaml_parser()
    test_workspace_variables_and_demand_selection()
    test_workspace_config_and_session_reuse()
    test_project_detection_and_resolution()
    test_notification_helpers()
    test_openspec_artifact_validation_and_state_guard()
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


def test_project_detection_and_resolution() -> None:
    with tempfile.TemporaryDirectory(prefix="sl-unit-project-") as tmp:
        root = Path(tmp)
        code_root = root / "code"
        service_a = code_root / "service-a"
        service_b = code_root / "service-b"
        service_a.mkdir(parents=True)
        service_b.mkdir(parents=True)
        (service_a / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "build": "vite build"}}), encoding="utf-8")
        (service_b / "go.mod").write_text("module example.com/service-b\n", encoding="utf-8")
        config = {"code_path": str(code_root), "verify_commands": {"service-b": "go test ./internal/..."}}
        selected, resolution = resolve_target_codebases(config, "# 限制条件\n- 修改的服务是 service-b\n")
        assert selected == [service_b.resolve()]
        assert resolution["matched_services"][0]["service_hint"] == "service-b"
        detected_a = detect_project(service_a, config)
        detected_b = detect_project(service_b, config)
        assert detected_a["language"] == "javascript"
        assert detected_a["verify_command"] == "npm test && npm run build"
        assert detected_b["language"] == "go"
        assert detected_b["verify_command"] == "go test ./internal/..."


def test_notification_helpers() -> None:
    session = {"session_id": "s1", "created_at": "2026-06-18T00:00:00Z", "started_at": "2026-06-18T00:00:00Z", "report_dir": "/tmp/report"}
    status = {"phase": "done", "current_task": "验证通过", "finished_at": "2026-06-18T00:01:00Z", "next_action": "归档"}
    assert workflow_notification_fingerprint(session, status, "通过") == "s1|通过|2026-06-18T00:01:00Z|done|验证通过"
    assert feishu_sign("secret", "123456").strip()
    payload = build_feishu_notification_payload(
        {"mode": "auto", "__workspace_root": "/tmp/work"},
        session,
        {"target_codebases": [{"name": "service-a"}], "todo_progress": {"completed_task_count": 1, "total_task_count": 1, "pending_task_count": 0}},
        status,
        "通过",
        "title",
    )
    assert payload["msg_type"] == "interactive"
    assert payload["card"]["header"]["template"] == "green"


def test_openspec_artifact_validation_and_state_guard() -> None:
    with tempfile.TemporaryDirectory(prefix="sl-unit-openspec-") as tmp:
        root = Path(tmp)
        home = root / "home"
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        workspace = root / "workspace"
        code = root / "code"
        change = workspace / "demands" / "1-demo" / "spec" / "openspec" / "changes" / "demo-change"
        (change / "specs").mkdir(parents=True)
        code.mkdir(parents=True)
        (code / "package.json").write_text(json.dumps({"scripts": {"test": "node -e 1"}}), encoding="utf-8")
        (workspace / "workspace.yml").write_text(
            "\n".join(
                [
                    "version: 1",
                    "workflow_source: openspec",
                    "mode: manual",
                    "code_path: ../code",
                    "openspec:",
                    "  change_name: demo-change",
                    "  changes_dir: demands/${demand_name}/spec/openspec/changes",
                    "demands:",
                    "  - name: 1-demo",
                    "    desc: demo",
                    "    workflow_source: openspec",
                    "    mode: manual",
                    "    demand_file: demands/${demand_name}/input/需求.md",
                    "    todo_file: demands/${demand_name}/spec/bridge/todo.md",
                    "    output_dir: demands/${demand_name}/rd/output",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (change / "proposal.md").write_text("# Proposal\n", encoding="utf-8")
        (change / "design.md").write_text("# Design\n", encoding="utf-8")
        (change / "tasks.md").write_text("# Tasks\n- [ ] 实现接口\n", encoding="utf-8")
        (change / "specs" / "api.md").write_text("# API\n", encoding="utf-8")
        config = load_workspace_config(workspace)
        validation = validate_openspec_change_artifacts(config)
        assert validation["valid"] is True
        baseline = {"tasks": {"sha256": "old"}}
        assert "tasks.md 自执行回写后已变化" in openspec_hash_drift(config, baseline)
        state = validate_sl_state(config, "apply")
        assert state["valid"] is False
        assert any("必须先执行 /sl:bridge" in item or "缺少桥接 todo.md" in item for item in state["errors"])


if __name__ == "__main__":
    main()
