# `/st:status`

用途：查看当前工作流状态，不修改代码和产物。

## 执行

1. 先执行公共 `route-check`。
2. 再执行：
   `python3 scripts/run-workflow.py route-st --command-text "/st:status"`

## 回复

只汇报：

- 当前 workflow_source / mode
- 当前 phase
- 当前 session
- 关键产物路径
- allowed_next
- blockers

禁止顺带执行下一阶段命令。
