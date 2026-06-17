# SpecLane User Guide

SpecLane is a structured AI delivery workflow. It helps an AI coding agent turn requirements and specs into traceable software changes with planning, implementation, self-check, review, verification, and archive checks.

## 1. Install

```bash
npm install -g @gary-coding/speclane@latest
speclane init
speclane sync --target both
```

`speclane init` creates a workspace template, installs local skill files when selected, and generates shortcut command templates for supported AI coding tools.

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
/sl:propose add-user-phone-filter
```

Review the generated OpenSpec change, then bridge tasks into `todo.md`:

```text
/sl:bridge
```

Review `todo.md` manually. After approval:

```text
/sl:apply
```

After verification passes, continue with:

```text
/sl:archive-check
/sl:archive
```

## 4. Todo Mode

Use todo mode for lightweight tasks or when `todo.md` already exists.

Write or review `todo.md`, then ask the AI agent:

```text
/sl:apply
```

In `auto` mode, SpecLane proceeds through planning, implementation handoff, self-check, review, and verification.

## 5. Command Reference

| Command | Purpose |
| --- | --- |
| `/sl:init` | Initialize or check the workspace |
| `/sl:propose <change-name>` | Generate or update an OpenSpec change without editing code |
| `/sl:bridge` | Convert OpenSpec `tasks.md` into reviewable `todo.md` |
| `/sl:plan` | Generate an implementation plan without editing code |
| `/sl:apply` | Start delivery and continue through implementation workflow |
| `/sl:review` | Run review stage separately |
| `/sl:verify` | Run verification and script-managed notification |
| `/sl:archive-check` | Check whether OpenSpec archive is safe |
| `/sl:archive` | Archive the OpenSpec change |
| `/sl:status` | Show current workflow status |

## 6. Language

SpecLane supports Chinese and English usage.

- The AI should reply in the same language as your latest message by default.
- You may explicitly ask: `Please respond in English` or `请使用中文`.
- Command names, file names, JSON keys, and CLI output keys stay unchanged.
