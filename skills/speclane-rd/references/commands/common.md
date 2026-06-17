# `/sl:*` 公共协议

`/sl:*` 是 SpecLane RD 的 AI 阶段命令，不是 shell 命令，也不是 OpenSpec `/opsx:*`。

## 语言 / Language

- 默认使用中文回复。
- 用户明确要求其他语言时再切换。
- 命令名、文件名、JSON key 和 CLI 输出 key 不翻译。

## 必做

1. 读取当前工作区 `workspace.yml`。
2. 如果命令包含 `--demand <demand-name>`，必须保留完整命令文本传给脚本。
3. 通过 `python3 scripts/run-workflow.py route-check --command-text "<用户命令>"` 做状态预检。
4. 预检通过后，再执行 `python3 scripts/run-workflow.py route-sl --command-text "<用户命令>"`。
5. 即使漏掉预检，`route-sl` 也会再次执行脚本级守卫；如果输出 `route_guard=blocked`，必须停止当前阶段。
6. 遵守脚本输出的 `final_reply_must` 或 `sl_reply_constraint_begin/end`。
7. 最终回复只汇报结果、阻塞点、允许的下一步和关键相对路径。

## 硬约束

- 禁止编辑 `workspace.yml`。
- 禁止手写 `.speclane/**/status.json`、`sl-state.json`、`current-session.json`、`plan.json`、`review.json`、`verify.json`、`notification.json`。
- 标准状态、报告、通知只能由脚本生成。
- 一次只执行用户当前明确请求的一个 `/sl:*` 命令。
- 状态异常、中断恢复、产物不一致时，先执行 `/sl:recover`，不要猜测下一步。
- 多需求模式下，每个需求状态位于 `.speclane/demands/<demand-name>/`，禁止跨需求复用 session。
- `/sl:propose` 之后只能提示 `/sl:bridge`。
- `/sl:bridge` 之后只能提示人工审核 `todo.md`，审核后 `/sl:apply`。
- `/sl:apply` 之前必须已经完成 bridge 且用户已审核 todo。
- 通知只能由 `run-workflow.py verify` 发送，AI 禁止直接调用飞书或 PushPlus webhook。

## 最小上下文

- `/sl:propose`：`workspace.yml`、`demand_file`、必要 `reference_files` 摘要。
- `/sl:bridge`：`tasks.md`、bridge context、`todo_file`。
- `/sl:plan`：`todo.md`、`discovery-summary` 或脚本生成的 plan 产物。
- `/sl:apply`：优先 `todo.md`、`plan-summary.json`、目标代码文件。
- `/sl:review` / `/sl:verify`：优先读取 summary 和脚本报告，不展开长 diff 或长日志。

命令细节只允许读取 `references/commands/` 下的对应命令文件。
