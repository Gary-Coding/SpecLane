# `sl` 命令协议

`/sl:*` 是用户发给 AI 的工作流指令，不是 shell 命令，也不是 OpenSpec `/opsx:*`。

用户只需要输入命令；AI 会根据当前 `workspace.yml`、状态机和 skill 协议调用底层脚本。

English: `/sl:*` commands are AI workflow instructions, not shell commands. The AI agent reads `workspace.yml`, validates the state machine, and calls SpecLane scripts.

## 语言 / Language

- 默认使用用户最新消息的语言回复。
- If the user writes in English, reply in English by default.
- 命令名、文件名、JSON key 和 CLI 输出 key 保持原样。

## 命令顺序

### todo 模式

```text
/sl:init
-> /sl:plan 或 /sl:apply
-> /sl:review
-> /sl:verify
```

`todo + auto` 通常可以直接从 `/sl:apply` 开始。

### OpenSpec 模式

```text
/sl:propose <change-name>
-> /sl:bridge
-> 人工审核 todo.md
-> /sl:apply
-> /sl:archive-check
-> /sl:archive
```

`/sl:propose` 后禁止直接 `/sl:apply`；必须先 `/sl:bridge`。

## 命令表

| 命令 | 作用 | 下一步 |
| --- | --- | --- |
| `/sl:init` | 初始化或检查工作区 | todo 模式可 `/sl:apply`，OpenSpec 模式可 `/sl:propose <change-name>` |
| `/sl:propose <change-name>` | 生成或修正 OpenSpec change，不改代码 | `/sl:bridge` |
| `/sl:bridge` | 将 `tasks.md` 桥接为待审核 `todo.md` | 审核后 `/sl:apply` |
| `/sl:plan` | 只生成实施计划，不改代码 | `/sl:apply` |
| `/sl:apply` | 进入交付，实现、自查，并在 auto 模式继续 review/verify | 失败则修复后重跑，通过后看状态 |
| `/sl:review` | 单独执行代码审查 | `/sl:verify` 或 `/sl:apply` 修复 |
| `/sl:verify` | 执行验证并由脚本发送通知 | OpenSpec 模式下一步 `/sl:archive-check` |
| `/sl:archive-check` | 检查 OpenSpec 是否可安全归档 | safe_merge 后 `/sl:archive` |
| `/sl:archive` | 归档 OpenSpec change 和相关 specs | 完成 |
| `/sl:status` | 查看当前状态和阻塞项 | 按 allowed_next 继续 |

## 最短提示词

OpenSpec 模式：

```text
/sl:propose add-user-phone-filter
```

```text
/sl:bridge
```

审核 `todo.md` 后：

```text
/sl:apply
```

todo 模式：

```text
/sl:apply
```

## 约束

- `workspace.yml` 是用户维护的契约，AI 禁止修改。
- 状态、报告、通知必须由脚本生成。
- 飞书/PushPlus 通知只能由 verify 脚本发送。
- OpenSpec `tasks.md` 在 bridge 后发生变化时，必须重新 `/sl:bridge`。
- 归档前必须先 `/sl:archive-check`。

AI 内部执行协议以 `skills/speclane-rd/references/commands/` 下的命令分片为准。
