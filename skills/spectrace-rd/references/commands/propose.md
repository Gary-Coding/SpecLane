# `/st:propose <change-name>`

用途：生成或修正当前 OpenSpec change 的规格文档，不改代码，不生成 todo。

## 前置

- `workflow_source=openspec`。
- 用户必须显式提供 `<change-name>`。
- 必须读取 `demand_file`，并读取真实存在的 `reference_files` 摘要作为上下文。

## 执行

1. 先执行公共 `route-check`。
2. 再执行：
   `python3 scripts/run-workflow.py route-st --command-text "/st:propose <change-name>"`
3. 根据 `propose-input.json` 更新或生成 `proposal.md`、`design.md`、`tasks.md`、`specs/`。
4. 停在 `proposed` 阶段。

## 禁止

- 禁止生成或覆盖 `todo.md`。
- 禁止执行 `/st:bridge`、`/st:plan`、`/st:apply`。
- 禁止提示“确认后执行 /st:apply”。

最终回复必须表达：代码未修改，下一步只能执行 `/st:bridge`。
