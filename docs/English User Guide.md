# SpecTrace User Guide

SpecTrace is a structured AI delivery workflow. It helps an AI coding agent turn requirements and specs into traceable software changes with planning, implementation, self-check, review, verification, and archive checks.

## 1. Install

```bash
npm install -g @gary-coding/spectrace@latest
spectrace init
spectrace sync --target both
```

`spectrace init` creates a workspace template, installs local skill files when selected, and generates shortcut command templates for supported AI coding tools.

## 2. Workspace

A workspace contains `workspace.yml`, requirement files, OpenSpec artifacts, generated `todo.md`, workflow state, and output reports.

Minimal OpenSpec mode:

```yaml
version: 1
mode: auto
workflow_source: openspec
vars:
  demand_name: 1-your-demand
demand_file: demands/${demand_name}/需求.md
todo_file: demands/${demand_name}/todo.md
reference_files: []
code_path: ../code
output_dir: demands/${demand_name}/output
openspec:
  changes_dir: openspec/changes
```

Minimal todo mode:

```yaml
version: 1
mode: auto
workflow_source: todo
todo_file: demands/1-your-demand/todo.md
reference_files: []
code_path: ../code
output_dir: demands/1-your-demand/output
```

## 3. OpenSpec Mode

Use OpenSpec mode for formal requirement iterations.

Ask the AI agent:

```text
/st:propose add-user-phone-filter
```

Review the generated OpenSpec change, then bridge tasks into `todo.md`:

```text
/st:bridge
```

Review `todo.md` manually. After approval:

```text
/st:apply
```

After verification passes, continue with:

```text
/st:archive-check
/st:archive
```

## 4. Todo Mode

Use todo mode for lightweight tasks or when `todo.md` already exists.

Write or review `todo.md`, then ask the AI agent:

```text
/st:apply
```

In `auto` mode, SpecTrace proceeds through planning, implementation handoff, self-check, review, and verification.

## 5. Command Reference

| Command | Purpose |
| --- | --- |
| `/st:init` | Initialize or check the workspace |
| `/st:propose <change-name>` | Generate or update an OpenSpec change without editing code |
| `/st:bridge` | Convert OpenSpec `tasks.md` into reviewable `todo.md` |
| `/st:plan` | Generate an implementation plan without editing code |
| `/st:apply` | Start delivery and continue through implementation workflow |
| `/st:review` | Run review stage separately |
| `/st:verify` | Run verification and script-managed notification |
| `/st:archive-check` | Check whether OpenSpec archive is safe |
| `/st:archive` | Archive the OpenSpec change |
| `/st:status` | Show current workflow status |

## 6. Language

SpecTrace supports Chinese and English usage.

- The AI should reply in the same language as your latest message by default.
- You may explicitly ask: `Please respond in English` or `请使用中文`.
- Command names, file names, JSON keys, and CLI output keys stay unchanged.
