---
name: speclane
description: SpecLane skill，用于 `/sl:*` AI 工程交付工作流命令。把 `/sl:*` 视为 AI 工作流命令，不是 shell 文本，也不是 OpenSpec `/opsx:*`。支持 todo 和 OpenSpec 桥接交付，包含状态机、计划、实现、自查、审查、验证、通知和归档。
---

# SpecLane

当用户发送 `/sl:*`，或要求使用 SpecLane 研发交付工作流时，使用这个 skill。

## 目录

这个 skill 是单一稳定入口，资源都在当前 skill 目录下：

```text
references/
modules/
scripts/
assets/
adapters/
```

长期形态按 PM / RD / QA 三阶段组织：

- `references/stages/pm.md`：产品需求梳理阶段约束。
- `references/stages/rd.md`：研发交付阶段约束，当前唯一可执行阶段。
- `references/stages/qa.md`：测试验证阶段约束。
- `modules/rd/`：RD 阶段细则。PM/QA 当前只保留阶段协议，不放空模块文档。

执行脚本时，相对当前 skill 目录解析：

```bash
python3 scripts/run-workflow.py route-check --command-text "<command>"
python3 scripts/run-workflow.py route-sl --command-text "<command>"
```

## 语言

- 默认使用中文回复。
- 用户明确要求其他语言时再切换。
- 命令名、文件名、JSON key、CLI 输出 key 不翻译。

## 命令路由

`/sl:*` 是发给 AI 的工作流指令，不是 shell 命令，也不能映射为 OpenSpec `/opsx:*`。

支持的命令：

- `/sl:init`
- `/sl:propose <change-name>`
- `/sl:bridge`
- `/sl:plan`
- `/sl:apply`
- `/sl:review`
- `/sl:verify`
- `/sl:archive-check`
- `/sl:archive`
- `/sl:status`
- `/sl:recover`
- `/sl:demand new|use|list|status <demand-name>`

当前可执行命令以 RD 交付为主；PM/QA 命令协议已预留但默认不执行代码变更。

命令细节只读取 [references/commands/common.md](references/commands/common.md) 和当前命令对应文件：

- `/sl:propose`: [references/commands/propose.md](references/commands/propose.md)
- `/sl:bridge`: [references/commands/bridge.md](references/commands/bridge.md)
- `/sl:plan`: [references/commands/plan.md](references/commands/plan.md)
- `/sl:apply`: [references/commands/apply.md](references/commands/apply.md)
- `/sl:review`: [references/commands/review.md](references/commands/review.md)
- `/sl:verify`: [references/commands/verify.md](references/commands/verify.md)
- `/sl:archive-check` 和 `/sl:archive`: [references/commands/archive.md](references/commands/archive.md)
- `/sl:status`: [references/commands/status.md](references/commands/status.md)
- `/sl:recover`: [references/commands/recover.md](references/commands/recover.md)
- `/sl:demand`: [references/commands/demand.md](references/commands/demand.md)
- 阶段命令总览：PM 见 [references/commands/pm.md](references/commands/pm.md)，RD 见 [references/commands/rd.md](references/commands/rd.md)，QA 见 [references/commands/qa.md](references/commands/qa.md)。

工作区规范读取 [references/workspace.md](references/workspace.md)。模式和产物细节按需读取 [references/workflow.md](references/workflow.md) 和 [references/execution-modes.md](references/execution-modes.md)。

RD 阶段需要更细规则时，按需读取：

- 计划定位：[modules/rd/planning.md](modules/rd/planning.md)
- 参考文件：[modules/rd/reference-files.md](modules/rd/reference-files.md)
- 审查门禁：[modules/rd/review.md](modules/rd/review.md)
- 验证门禁：[modules/rd/verify.md](modules/rd/verify.md)
- 技术栈识别：[modules/rd/tech-stacks.md](modules/rd/tech-stacks.md)

## 最小执行步骤

1. 读取 `<workspace>/workspace.yml`。
2. 识别 `workflow_source`、`mode`、`todo_file`、`demand_file`、`reference_files`、`code_path`、`output_dir` 和可选 `openspec` 字段。
3. 如果用户命令包含 `--demand <demand-name>`，必须把完整命令文本传给脚本，不能丢失 demand 参数。
4. 能预检时，先执行 `python3 scripts/run-workflow.py route-check --command-text "<command>"`。
5. 预检通过后，执行 `python3 scripts/run-workflow.py route-sl --command-text "<command>"`。
6. 如果状态不一致或上次执行被中断，先执行 `/sl:recover`，再决定下一阶段。
7. 遵守脚本状态校验和脚本输出的回复约束。
8. 最终回复保持简洁，只汇报结果、阻塞点、允许下一步和关键相对路径。

## 硬约束

- 禁止编辑 `<workspace>/workspace.yml`，它是用户维护的契约。
- 禁止手工创建、编辑或伪造 `.speclane/current-session.json`、`.speclane/sl-state.json`、`.speclane/sessions/**/status.json`、`plan.json`、`review.json`、`verify.json`、`notification.json` 或标准 output 报告。
- 标准 JSON 和 Markdown 产物必须由工作流脚本生成。
- 一次只执行用户当前明确请求的一个 `/sl:*` 命令，下一步命令只能作为文字建议。
- 多需求模式下，每个需求的状态和 session 隔离在 `.speclane/demands/<demand_name>/`；AI 禁止跨需求复用 session。
- 如果脚本输出 `final_reply_must` 或 `sl_reply_constraint_begin` / `sl_reply_constraint_end`，最终回复必须遵守。
- `auto` 只在 `/sl:apply` 内部生效；`/sl:init`、`/sl:propose`、`/sl:bridge`、`/sl:plan` 都必须在自身阶段结束后停止。
- OpenSpec 模式下，`/sl:propose <change-name>` 必须显式指定 change 名称，不能从需求标题或 `demand_name` 推导。
- OpenSpec 模式下，只有显式 `/sl:bridge` 可以生成或覆盖 `todo_file`。
- `/sl:bridge` 生成的 `todo.md` 必须经用户审核后才能 `/sl:apply`。
- `/sl:propose` 不能调用 bridge 或修改 `todo_file`；下一步只能是 `/sl:bridge`。
- `/sl:bridge` 不能调用 plan/apply/review/verify，也不能修改代码。
- `/sl:verify` 通过前禁止建议 `/sl:archive-check`。
- `/sl:archive-check` 结果为 `safe_merge` 前禁止建议 `/sl:archive`。
- 通知只能由 `python3 scripts/run-workflow.py verify` 发送，AI 禁止直接调用飞书或 PushPlus webhook。
- `notification.json` 是唯一通知成功证据。

## 上下文纪律

- 优先汇报摘要和产物路径，不在对话中粘贴完整大文件。
- `reference_files` 是强上下文，但脚本默认会摘要大文件；只有当前命令确实需要细节时才读取全文。
- OpenSpec 模式下，除非当前阶段需要，不要反复读取 `proposal.md`、`design.md` 和 `specs/**/*.md` 全文。
- 后续阶段优先读取 `plan-summary.json`，只有需要详细计划数据时再读取 `plan.json`。
- 优先读取 `discovery-summary.json`，只有需要详细代码证据时再读取 `discovery.json`。
- 需要机器可读执行摘要时，优先使用 `route-sl --json`。
- 最终回复保持紧凑，详细内容沉淀到 `output_dir/<session_id>/`。
