# QA 命令协议

QA 命令用于在 RD 交付完成后生成测试阶段产物。当前为最小可用版本，不接入 RD 默认状态机。

可用命令：

```text
/sl:qa:plan
/sl:qa:report
```

当前处理规则：

- `/sl:qa:plan` 基于 RD 输出、todo.md、OpenSpec change 和参考文档生成 `demands/<demand-name>/qa/test-plan.md`。
- `/sl:qa:report` 生成 `demands/<demand-name>/qa/test-report.md` 草稿，测试人员需要补充真实执行结果。
- 不要修改 RD 的标准状态文件。
- 不要伪造测试通过结论。
- QA 发现缺陷时，应形成缺陷记录，并回到 RD 修复链路或创建新的需求实例。
