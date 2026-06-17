# `/sl:apply`

用途：进入交付阶段，按计划实现、生成自查，并在 auto 模式继续 review/verify。

## 前置

- todo 模式：存在有效 `todo.md`。
- openspec 模式：已经 `/sl:bridge`，且用户已审核 `todo.md`。
- 如无计划，脚本可创建或复用标准 session 并生成计划。
- OpenSpec `tasks.md` hash 必须与最近 bridge 记录一致。

## 执行

1. 先执行公共 `route-check`。
2. 再执行：
   `python3 scripts/run-workflow.py route-sl --command-text "/sl:apply"`
3. AI 按当前 `plan-summary.json` / `plan.json` 修改目标代码。
4. 修改完成后调用 `finish-implement`，由脚本生成 self-check。
5. auto 模式继续由标准脚本推进 review、verify 和通知。

## 禁止

- 禁止第二次手工执行 `/sl:plan` 创建新 session。
- 禁止手写状态文件或 output 报告。
- 禁止直接发飞书/PushPlus 通知。
- verify 通过前禁止提示 archive。

最终回复汇报改动、review、verify、通知和允许的下一步。
