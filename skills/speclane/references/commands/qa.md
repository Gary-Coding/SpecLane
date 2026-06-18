# QA 命令协议

QA 命令为长期形态预留，当前不接入默认状态机。

建议命名：

```text
/sl:qa:plan
/sl:qa:run
/sl:qa:report
/sl:qa:regress
```

当前处理规则：

- 如果用户请求 QA 阶段，先说明当前版本仅预留协议。
- 可以基于 RD 输出和 OpenSpec change 帮用户整理测试计划或测试用例。
- 不要修改 RD 的标准状态文件。
- 不要伪造测试通过结论。
