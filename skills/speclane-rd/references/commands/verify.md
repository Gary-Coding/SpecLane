# `/sl:verify`

用途：运行验证命令、生成 verify 报告，并由脚本发送通知。

## 前置

- 已完成 review。
- 存在标准 session。

## 执行

1. 先执行公共 `route-check`。
2. 再执行：
   `python3 scripts/run-workflow.py route-sl --command-text "/sl:verify"`
3. 验证日志由脚本截断摘要。
4. 通知只能由 verify 脚本发送，成功证据是 `notification.json`。

## 禁止

- 禁止 AI 直接调用任何 webhook。
- verify 失败时禁止提示 archive-check。

OpenSpec 模式 verify 通过后，下一步 `/sl:archive-check`。
