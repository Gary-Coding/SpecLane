# `/st:bridge`

用途：把当前 OpenSpec change 的 `tasks.md` 转成待审核 `todo.md`。

## 前置

- 已有 active OpenSpec change。
- 已存在 `proposal.md`、`design.md`、`tasks.md`。
- 当前阶段为 `proposed` 或允许重新 bridge 的 `bridged`。

## 执行

1. 先执行公共 `route-check`。
2. 再执行：
   `python3 scripts/run-workflow.py route-st --command-text "/st:bridge"`
3. 脚本生成 `todo.md`，并记录 `tasks.md` hash。
4. 停在 `bridged` 阶段。

## 禁止

- 禁止手工同步 `tasks.md` 到 `todo.md`。
- 禁止自动进入 `/st:plan` 或 `/st:apply`。
- 如果审核后发现需求或 todo 有偏差，先修正需求或重新 `/st:propose <change-name>`，再重新 `/st:bridge`。

最终回复只能提示：请人工审核 `todo.md`，审核通过后发送 `/st:apply`。
