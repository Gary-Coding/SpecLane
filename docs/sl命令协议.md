# `sl` 命令协议

`/sl:*` 是用户发给 AI 的工作流指令，不是 shell 命令，也不是 OpenSpec `/opsx:*`。

用户只需要输入命令；AI 会根据当前 `workspace.yml`、状态机和 skill 协议调用底层脚本。

## 语言

- 默认使用用户最新消息的语言回复。
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
| `/sl:recover` | 从标准产物恢复工作流状态视图 | 按 allowed_next 继续 |
| `/sl:demand new <name>` | 新建需求实例并设为 active demand | `/sl:propose <change> --demand <name>` 或 `/sl:apply --demand <name>` |
| `/sl:demand use <name>` | 切换 active demand | 后续命令可省略 `--demand` |
| `/sl:demand list` | 查看工作区所有需求实例 | 按需切换或查看状态 |
| `/sl:demand status <name>` | 查看指定需求状态 | 按 allowed_next 继续 |
| `/sl:qa:plan` | 基于 RD 输出生成测试计划 | 测试人员审核并补充测试用例 |
| `/sl:qa:report` | 生成测试报告草稿 | 测试人员补充真实执行结果 |

多需求模式下，主流程命令可以追加 demand 参数：

```text
/sl:propose add-user-phone-filter --demand demand-a
/sl:bridge --demand demand-a
/sl:apply --demand demand-a
```

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
- 状态异常、中断恢复、产物不一致时，先执行 `/sl:recover`，不要猜测下一步。
- 多需求模式下，每个需求状态位于 `.speclane/demands/<demand-name>/`，AI 禁止跨需求复用 session。
- `/sl:qa:*` 是 QA 旁路命令，只写入 `demands/<demand-name>/qa/`，不改变 RD 状态机。

AI 内部执行协议以 `skills/speclane/references/commands/` 下的命令分片为准；脚本统一来自 `skills/speclane/scripts/`。对普通用户稳定的是 `speclane` CLI 和 `/sl:*` 命令，内部 Python 脚本命令不作为兼容 API 承诺。
