#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path

from common import (
    openspec_archive_root,
    openspec_change_dir,
    openspec_writeback_dir,
    read_json,
    update_st_state,
    workflow_source,
    workspace_root,
    load_workspace_config,
    write_managed_json,
    write_managed_text,
)


def sync_specs(change_dir: Path) -> list[dict[str, str]]:
    delta_root = change_dir / "specs"
    if not delta_root.exists():
        return []
    repo_openspec_root = change_dir.parent.parent
    target_specs_root = repo_openspec_root / "specs"
    synced: list[dict[str, str]] = []
    for source in sorted(delta_root.rglob("*.md")):
        relative = source.relative_to(delta_root)
        target = target_specs_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        synced.append({"source": str(source), "target": str(target)})
    return synced


def build_markdown(payload: dict) -> str:
    lines = [
        "# Archive Result",
        "",
        "## Summary",
        f"- change: {payload.get('change_name', '')}",
        f"- archived_to: {payload.get('archived_to', '')}",
        "",
        "## Synced Specs",
    ]
    synced = payload.get("synced_specs", [])
    if synced:
        lines.extend(f"- {item.get('source', '')} -> {item.get('target', '')}" for item in synced)
    else:
        lines.append("- 暂无")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="归档 OpenSpec change，并把 delta specs 合并回主 specs。")
    parser.add_argument("--workspace", help="工作空间路径，默认读取当前目录")
    args = parser.parse_args()

    workspace = workspace_root(Path(args.workspace).expanduser() if args.workspace else None)
    config = load_workspace_config(workspace)
    if workflow_source(config) != "openspec":
        raise SystemExit("当前 workspace.yml 未启用 OpenSpec 模式，无需执行 archive-openspec。")

    change_dir = openspec_change_dir(config)
    writeback_dir = openspec_writeback_dir(config)
    archive_input = read_json(writeback_dir / "archive-input.json", {})
    if not archive_input:
        raise SystemExit(f"未找到 archive-input.json：{writeback_dir / 'archive-input.json'}")
    if not archive_input.get("archive_ready", False):
        blockers = ", ".join(archive_input.get("blockers", []))
        raise SystemExit(f"当前 change 尚未满足归档条件：{blockers}")
    if str(archive_input.get("merge_mode", "")).strip() != "safe_merge":
        raise SystemExit("当前 change 不是 safe_merge，需先人工处理 spec 冲突。")
    if archive_input.get("spec_conflicts"):
        raise SystemExit("检测到 spec 冲突，归档已中止。")

    synced_specs = sync_specs(change_dir)
    archive_root = openspec_archive_root(config)
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_name = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{change_dir.name}"
    archive_target = archive_root / archive_name
    if archive_target.exists():
        raise SystemExit(f"归档目标已存在：{archive_target}")
    shutil.move(str(change_dir), str(archive_target))

    payload = {
        "change_name": change_dir.name,
        "archived_to": str(archive_target),
        "synced_specs": synced_specs,
    }
    write_managed_json(config, archive_target / "spectrace" / "archive-result.json", payload)
    write_managed_text(config, archive_target / "spectrace" / "archive-result.md", build_markdown(payload))
    update_st_state(
        config,
        phase="archived",
        last_command="/st:archive",
        artifacts={
            "archive_result": str(archive_target / "spectrace" / "archive-result.json"),
            "archived_to": str(archive_target),
        },
    )
    print(f"archived_to={archive_target}")


if __name__ == "__main__":
    main()
