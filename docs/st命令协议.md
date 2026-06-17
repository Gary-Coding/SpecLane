# `st` 命令协议

`/st:*` 是用户发给 AI 的工作流指令，不是 shell 命令，也不是 OpenSpec `/opsx:*`。

用户只需要输入命令；AI 会根据当前 `workspace.yml`、状态机和 skill 协议调用底层脚本。

English: `/st:*` commands are AI workflow instructions, not shell commands. The AI agent reads `workspace.yml`, validates the state machine, and calls SpecTrace scripts.

## 语言 / Language

- 默认使用用户最新消息的语言回复。
- If the user writes in English, reply in English by default.
- 命令名、文件名、JSON key 和 CLI 输出 key 保持原样。

## 命令顺序

### todo 模式

```text
/st:init
-> /st:plan 或 /st:apply
-> /st:review
-> /st:verify
```

`todo + auto` 通常可以直接从 `/st:apply` 开始。

### OpenSpec 模式

```text
/st:propose <change-name>
-> /st:bridge
-> 人工审核 todo.md
-> /st:apply
-> /st:archive-check
-> /st:archive
```

`/st:propose` 后禁止直接 `/st:apply`；必须先 `/st:bridge`。

## 命令表

| 命令 | 作用 | 下一步 |
| --- | --- | --- |
| `/st:init` | 初始化或检查工作区 | todo 模式可 `/st:apply`，OpenSpec 模式可 `/st:propose <change-name>` |
| `/st:propose <change-name>` | 生成或修正 OpenSpec change，不改代码 | `/st:bridge` |
| `/st:bridge` | 将 `tasks.md` 桥接为待审核 `todo.md` | 审核后 `/st:apply` |
| `/st:plan` | 只生成实施计划，不改代码 | `/st:apply` |
| `/st:apply` | 进入交付，实现、自查，并在 auto 模式继续 review/verify | 失败则修复后重跑，通过后看状态 |
| `/st:review` | 单独执行代码审查 | `/st:verify` 或 `/st:apply` 修复 |
| `/st:verify` | 执行验证并由脚本发送通知 | OpenSpec 模式下一步 `/st:archive-check` |
| `/st:archive-check` | 检查 OpenSpec 是否可安全归档 | safe_merge 后 `/st:archive` |
| `/st:archive` | 归档 OpenSpec change 和相关 specs | 完成 |
| `/st:status` | 查看当前状态和阻塞项 | 按 allowed_next 继续 |

## 最短提示词

OpenSpec 模式：

```text
/st:propose add-user-phone-filter
```

```text
/st:bridge
```

审核 `todo.md` 后：

```text
/st:apply
```

todo 模式：

```text
/st:apply
```

## 约束

- `workspace.yml` 是用户维护的契约，AI 禁止修改。
- 状态、报告、通知必须由脚本生成。
- 飞书/PushPlus 通知只能由 verify 脚本发送。
- OpenSpec `tasks.md` 在 bridge 后发生变化时，必须重新 `/st:bridge`。
- 归档前必须先 `/st:archive-check`。

AI 内部执行协议以 `skills/spectrace-rd/references/commands/` 下的命令分片为准。
