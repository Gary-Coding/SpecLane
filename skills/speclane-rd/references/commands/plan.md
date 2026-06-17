# `/sl:plan`

用途：只生成实施计划，不改代码。

## 前置

- todo 模式：存在有效 `todo.md`。
- openspec 模式：已经 `/sl:bridge`，且用户已审核 `todo.md`。
- 当前不存在已进入实现、review、verify 的活跃 session。

## 执行

1. 先执行公共 `route-check`。
2. 再执行：
   `python3 scripts/run-workflow.py route-sl --command-text "/sl:plan"`
3. 脚本生成 `discovery.json`、`plan.json`、`plan-summary.json`、`plan.md`。
4. 如果已有可复用计划 session，必须复用，不能新建空 session。

## 禁止

- 禁止改代码。
- 禁止继续 review/verify。
- OpenSpec `tasks.md` hash 与 bridge 记录不一致时，必须停止并要求重新 `/sl:bridge`。

最终回复提示下一步 `/sl:apply`。
