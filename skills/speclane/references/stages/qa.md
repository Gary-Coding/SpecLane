# QA 阶段

QA 阶段面向测试设计、测试执行、缺陷回归和验收结论。

## 当前状态

当前阶段为协议预留，默认不执行脚本，不修改代码。

## 输入

- RD 输出报告：`demands/<demand-name>/rd/output/<session-id>/`
- OpenSpec change：`demands/<demand-name>/spec/openspec/changes/<change-name>/`
- 桥接 todo：`demands/<demand-name>/spec/bridge/todo.md`

## 输出

推荐输出到：

```text
demands/<demand-name>/qa/
```

可包含：

- `test-plan.md`
- `test-cases.md`
- `test-report.md`
- `defects.md`
- `regression.md`

## 边界

- QA 阶段不重写 RD 计划。
- QA 阶段发现缺陷时，应形成缺陷记录或触发新的 RD 修复需求。
- QA 通过后才建议进入团队归档或发布记录沉淀。
