# PM 命令协议

PM 命令为长期形态预留，当前不接入默认状态机。

建议命名：

```text
/sl:pm:brief
/sl:pm:review
/sl:pm:handoff
```

当前处理规则：

- 如果用户请求 PM 阶段，先说明当前版本仅预留协议。
- 可以基于 `input/需求.md` 帮用户整理文档，但不要调用 RD 脚本。
- 不要生成或覆盖 `spec/bridge/todo.md`。
- 不要修改业务代码。
