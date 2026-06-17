#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_WORKFLOW = REPO_ROOT / "skills" / "speclane-rd" / "scripts" / "run-workflow.py"
CLI = REPO_ROOT / "bin" / "speclane.js"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sl-e2e-") as tmp:
        root = Path(tmp)
        home = root / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        test_templates_cli(root)
        test_openspec_state_and_bridge(root)
        test_incomplete_plan_session_is_reused(root)
        test_todo_auto_session_and_verify_compaction(root)
    print("e2e_test=ok")


def test_templates_cli(root: Path) -> None:
    workspace = root / "template-workspace"
    output = run(["node", str(CLI), "templates"])
    if "openspec-auto" not in output or "todo-auto" not in output:
        raise AssertionError("templates list did not include expected templates")

    show_output = run(["node", str(CLI), "template", "show", "openspec-auto"])
    if "workflow_source: openspec" not in show_output or "mode: auto" not in show_output:
        raise AssertionError("template show returned unexpected content")

    run(
        [
            "node",
            str(CLI),
            "template",
            "copy",
            "todo-auto",
            "--workspace",
            str(workspace),
            "--demand-name",
            "9-e2e-template",
            "--code-path",
            "../code",
        ]
    )
    workspace_yml = read_text(workspace / "workspace.yml")
    if "workflow_source: todo" not in workspace_yml or "9-e2e-template" not in workspace_yml:
        raise AssertionError("template copy did not render workspace.yml")

    doctor_before = run(["node", str(CLI), "doctor", "--workspace", str(workspace)], check=False)
    if "workspace.commands.sl" not in doctor_before.output:
        raise AssertionError("doctor should report workspace command status")
    run(["node", str(CLI), "doctor", "--workspace", str(workspace), "--fix"], check=False)
    if not (workspace / ".claude" / "commands" / "sl" / "apply.md").exists():
        raise AssertionError("doctor --fix should create sl command templates")
    run(["node", str(CLI), "commands", "install", "--workspace", str(workspace), "--target", "all"])
    for command_path in [
        workspace / ".cursor" / "commands" / "sl" / "apply.md",
        workspace / ".trae" / "commands" / "sl" / "apply.md",
        workspace / ".kimi" / "commands" / "sl" / "apply.md",
        home_prompt(root) / "sl-apply.md",
    ]:
        if not command_path.exists():
            raise AssertionError(f"commands install all missing {command_path}")


def test_openspec_state_and_bridge(root: Path) -> None:
    workspace = root / "openspec-workspace"
    code = root / "code" / "demo-service"
    demand_dir = workspace / "demands" / "8-demo"
    demand_dir.mkdir(parents=True)
    code.mkdir(parents=True)
    (code / "package.json").write_text(
        json.dumps(
            {
                "name": "demo-service",
                "version": "1.0.0",
                "scripts": {"test": "node -e \"process.exit(0)\""},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (workspace / "docs").mkdir(parents=True)
    (workspace / "openspec" / "changes").mkdir(parents=True)
    (workspace / "openspec" / "specs").mkdir(parents=True)
    (demand_dir / "需求.md").write_text(
        "# 需求\n\n为 demo-service 增加状态查询接口。\n\n## 验收\n\n- 查询接口返回 ok。\n",
        encoding="utf-8",
    )
    (workspace / "workspace.yml").write_text(
        "\n".join(
            [
                "version: 1",
                "mode: auto",
                "workflow_source: openspec",
                "vars:",
                "  demand_name: 8-demo",
                "demand_file: demands/${demand_name}/需求.md",
                "todo_file: demands/${demand_name}/todo.md",
                "reference_files: []",
                "code_path: ../code/demo-service",
                "output_dir: demands/${demand_name}/output",
                "openspec:",
                "  changes_dir: openspec/changes",
                "",
            ]
        ),
        encoding="utf-8",
    )

    invalid = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:bridge",
        ],
        check=False,
    )
    if invalid.returncode == 0 or "请先执行 /sl:propose" not in invalid.output:
        raise AssertionError("/sl:bridge before /sl:propose should be rejected")

    env_without_openspec = os.environ.copy()
    env_without_openspec["PATH"] = ""
    propose = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:propose demo-change",
        ],
        env=env_without_openspec,
    )
    if "final_reply_must=代码未修改。下一步只能执行 /sl:bridge。" not in propose:
        raise AssertionError("/sl:propose did not print strict next-step constraint")

    change_dir = workspace / "openspec" / "changes" / "demo-change"
    (change_dir / "specs").mkdir(parents=True, exist_ok=True)
    (change_dir / "proposal.md").write_text("# Proposal\n\n增加状态查询接口。\n", encoding="utf-8")
    (change_dir / "design.md").write_text("# Design\n\n目标服务 demo-service。\n", encoding="utf-8")
    (change_dir / "tasks.md").write_text(
        "# Tasks\n\n"
        "- [ ] 修改 demo-service controller 增加状态查询接口\n"
        "- [ ] 补充验证，确认接口返回 ok\n",
        encoding="utf-8",
    )

    bridge = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:bridge",
        ]
    )
    if "bridge_generated=true" not in bridge:
        raise AssertionError("/sl:bridge did not generate todo.md")
    todo = read_text(workspace / "demands" / "8-demo" / "todo.md")
    if "demo-change" not in todo or "demo-service" not in todo:
        raise AssertionError("bridged todo.md missing expected OpenSpec context")

    route_check = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-check",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:apply",
        ]
    )
    route_payload = json.loads(route_check)
    if not route_payload.get("allowed") or route_payload.get("run_command") != "apply":
        raise AssertionError("route-check should return allowed JSON for bridged /sl:apply")

    (change_dir / "tasks.md").write_text(
        "# Tasks\n\n"
        "- [ ] 修改 demo-service controller 增加状态查询接口\n"
        "- [ ] 补充验证，确认接口返回 ok\n"
        "- [ ] 追加变更后必须重新桥接\n",
        encoding="utf-8",
    )
    stale_apply = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:apply",
        ],
        check=False,
    )
    if stale_apply.returncode == 0 or "tasks.md 已变化" not in stale_apply.output:
        raise AssertionError("changed tasks.md should require /sl:bridge before /sl:apply")

    bridge_again = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:bridge",
        ]
    )
    if "tasks_sha256=" not in bridge_again:
        raise AssertionError("second /sl:bridge should record tasks hash")

    first_plan = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:plan",
        ]
    )
    if "session_action=created" not in first_plan:
        raise AssertionError("first /sl:plan should create a session")
    first_session = read_json(workspace / ".speclane" / "current-session.json")
    first_session_id = first_session["session_id"]
    data_dir = Path(first_session["data_dir"])
    if not (data_dir / "discovery-summary.json").exists():
        raise AssertionError("plan should create discovery-summary.json")

    second_plan = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:plan",
        ]
    )
    second_session = read_json(workspace / ".speclane" / "current-session.json")
    if "session_action=reused" not in second_plan or second_session["session_id"] != first_session_id:
        raise AssertionError("second /sl:plan should reuse the existing planning session")
    if len(list((workspace / ".speclane" / "sessions").iterdir())) != 1:
        raise AssertionError("repeated /sl:plan created an extra session")

    apply_output = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:apply",
        ]
    )
    if "apply_phase=implementing" not in apply_output:
        raise AssertionError("/sl:apply should enter implementing after planned session")
    apply_json_output = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:apply",
            "--json",
        ],
        check=False,
    )
    if "route_result_json_begin" not in apply_json_output.output and "当前状态不允许进入实现" not in apply_json_output.output:
        raise AssertionError("route-sl --json should emit JSON summary or a state guard")
    blocked_plan = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:plan",
        ],
        check=False,
    )
    final_session = read_json(workspace / ".speclane" / "current-session.json")
    if blocked_plan.returncode == 0 or "不能重新执行 /sl:plan" not in blocked_plan.output:
        raise AssertionError("/sl:plan during active delivery should be rejected")
    if final_session["session_id"] != first_session_id or len(list((workspace / ".speclane" / "sessions").iterdir())) != 1:
        raise AssertionError("rejected /sl:plan should not create or switch sessions")


def test_todo_auto_session_and_verify_compaction(root: Path) -> None:
    workspace = root / "todo-workspace"
    code = root / "todo-code" / "demo-service"
    demand_dir = workspace / "demands" / "9-demo"
    demand_dir.mkdir(parents=True)
    code.mkdir(parents=True)
    (workspace / "docs").mkdir(parents=True)
    (code / "verify.py").write_text(
        "print('A' * 13000)\n"
        "import sys\n"
        "print('B' * 13000, file=sys.stderr)\n",
        encoding="utf-8",
    )
    (code / "package.json").write_text(
        json.dumps(
            {
                "name": "demo-service",
                "version": "1.0.0",
                "scripts": {"test": "node -e \"process.exit(0)\""},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    verify_command = f'"{sys.executable}" verify.py'
    (workspace / "workspace.yml").write_text(
        "\n".join(
            [
                "version: 1",
                "mode: auto",
                "workflow_source: todo",
                "vars:",
                "  demand_name: 9-demo",
                "todo_file: demands/${demand_name}/todo.md",
                "reference_files: []",
                "code_path: ../todo-code/demo-service",
                "output_dir: demands/${demand_name}/output",
                "verify_commands:",
                f"  default: {verify_command}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (demand_dir / "todo.md").write_text(
        "# 限制条件\n"
        "- 修改的服务是 demo-service\n\n"
        "# 待办事项\n\n"
        "- [ ] 增加状态查询接口\n"
        "1. 返回 ok\n\n"
        "## 验收补充\n"
        "- [ ] 执行验证命令\n",
        encoding="utf-8",
    )

    apply_output = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:apply",
        ]
    )
    if "apply_phase=implementing" not in apply_output:
        raise AssertionError("/sl:apply did not enter implementing phase")

    run([sys.executable, str(RUN_WORKFLOW), "finish-implement", "--workspace", str(workspace)])
    session = read_json(workspace / ".speclane" / "current-session.json")
    data_dir = Path(session["data_dir"])
    plan_summary = read_json(data_dir / "plan-summary.json")
    verify = read_json(data_dir / "verify.json")
    status = read_json(data_dir / "status.json")
    notification = read_json(data_dir / "notification.json")

    if not plan_summary.get("target_codebases"):
        raise AssertionError("plan-summary.json missing target_codebases")
    if verify.get("result") != "通过":
        raise AssertionError("verify did not pass")
    stdout = verify["sections"][0]["stdout"]
    stderr = verify["sections"][0]["stderr"]
    if "已省略" not in stdout or "已省略" not in stderr:
        raise AssertionError("verify stdout/stderr were not compacted")
    if len(stdout) > 12200 or len(stderr) > 12200:
        raise AssertionError("verify stdout/stderr compaction exceeded expected size")
    if status.get("phase") != "done":
        raise AssertionError("todo auto session did not finish with done status")
    if notification.get("status") != "skipped":
        raise AssertionError("notification should be marked skipped when no provider is configured")


def test_incomplete_plan_session_is_reused(root: Path) -> None:
    workspace = root / "incomplete-plan-workspace"
    code = root / "broken-code" / "not-a-project"
    demand_dir = workspace / "demands" / "10-broken"
    demand_dir.mkdir(parents=True)
    code.mkdir(parents=True)
    (workspace / "docs").mkdir(parents=True)
    (workspace / "workspace.yml").write_text(
        "\n".join(
            [
                "version: 1",
                "mode: auto",
                "workflow_source: todo",
                "vars:",
                "  demand_name: 10-broken",
                "todo_file: demands/${demand_name}/todo.md",
                "reference_files: []",
                "code_path: ../broken-code/not-a-project",
                "output_dir: demands/${demand_name}/output",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (demand_dir / "todo.md").write_text(
        "# 限制条件\n"
        "- 修改的服务是 broken-service\n\n"
        "# 待办事项\n\n"
        "- [ ] 增加一个测试接口\n",
        encoding="utf-8",
    )

    first_apply = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:apply",
        ],
        check=False,
    )
    if first_apply.returncode == 0 or "未找到可识别的项目目录" not in first_apply.output:
        raise AssertionError("first /sl:apply should fail before plan is generated")
    current = read_json(workspace / ".speclane" / "current-session.json")
    first_session_id = current["session_id"]
    sessions_dir = workspace / ".speclane" / "sessions"
    output_dir = workspace / "demands" / "10-broken" / "output"
    if len(list(sessions_dir.iterdir())) != 1:
        raise AssertionError("first failed /sl:apply should create exactly one reusable data session")
    if output_dir.exists() and list(output_dir.iterdir()):
        raise AssertionError("failed plan should not create an empty output report session")

    second_apply = run(
        [
            sys.executable,
            str(RUN_WORKFLOW),
            "route-sl",
            "--workspace",
            str(workspace),
            "--command-text",
            "/sl:apply",
        ],
        check=False,
    )
    if second_apply.returncode == 0 or "session_action=reused_incomplete" not in second_apply.output:
        raise AssertionError("second /sl:apply should reuse the incomplete planning session")
    current = read_json(workspace / ".speclane" / "current-session.json")
    if current["session_id"] != first_session_id:
        raise AssertionError("incomplete planning session should remain current")
    if len(list(sessions_dir.iterdir())) != 1:
        raise AssertionError("repeated failed /sl:apply should not create another data session")
    if output_dir.exists() and list(output_dir.iterdir()):
        raise AssertionError("repeated failed /sl:apply should not create output report sessions")


def run(command: list[str], check: bool = True, env: dict[str, str] | None = None):
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if check:
        if result.returncode != 0:
            print(result.stdout)
            raise SystemExit(result.returncode)
        return result.stdout
    return CommandResult(result.returncode, result.stdout)


def home_prompt(root: Path) -> Path:
    return root / "home" / ".codex" / "prompts"


class CommandResult:
    def __init__(self, returncode: int, output: str) -> None:
        self.returncode = returncode
        self.output = output


def read_text(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"missing file: {path}")
    return path.read_text(encoding="utf-8")


def read_json(path: Path) -> dict:
    if not path.is_file():
        raise AssertionError(f"missing file: {path}")
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise AssertionError(f"json root is not object: {path}")
    return data


if __name__ == "__main__":
    if shutil.which("node") is None:
        raise SystemExit("node is required")
    main()
