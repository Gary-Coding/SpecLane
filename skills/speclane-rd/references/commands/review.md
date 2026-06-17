# `/sl:review`

用途：基于标准 session 和真实代码差异生成 review 结果。

## 前置

- 已完成实现和 self-check。
- 存在标准 `plan.json` / `plan-summary.json`。

## 执行

1. 先执行公共 `route-check`。
2. 再执行：
   `python3 scripts/run-workflow.py route-sl --command-text "/sl:review"`
3. 优先读取 `plan-summary.json`，输出 compact review。

## 禁止

- 禁止在未实现完成时 review。
- 禁止跳过 blocking finding 进入 verify。

review 通过时下一步 `/sl:verify`；存在 blocking finding 时下一步 `/sl:apply` 修复。
