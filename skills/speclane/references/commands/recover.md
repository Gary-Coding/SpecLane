# `/sl:recover`

用途：从标准工作流产物恢复状态视图，帮助中断、失败或 AI 误跳阶段后重新判断下一步。

## 前置

- 已存在 `workspace.yml`。
- 不要求当前 session 完整。
- 不修改业务代码。

## 执行

1. 先执行公共 `route-check`。
2. 再执行：
   `python3 scripts/run-workflow.py route-sl --command-text "/sl:recover"`
3. 根据输出的 `phase`、`allowed_next`、`blocked_reason` 和 `artifact.*` 汇报当前状态。

## 禁止

- 禁止手写或手工修复 `.speclane/**` 状态文件。
- 禁止在 recover 后自动执行下一阶段。
- 禁止修改业务代码。

最终回复只说明恢复后的阶段、允许的下一步、发现的阻塞点和关键相对路径。
