# RD 阶段

RD 阶段面向研发交付，是当前唯一完整可执行阶段。

## 输入

OpenSpec 模式：

- `demands/<demand-name>/input/需求.md`
- `demands/<demand-name>/spec/openspec/changes/<change-name>/tasks.md`
- `demands/<demand-name>/spec/bridge/todo.md`

todo 模式：

- `demands/<demand-name>/spec/bridge/todo.md`

## 流程

```text
propose -> bridge -> plan -> implement -> self-check -> review -> verify -> archive-check -> archive
```

## 输出

人可读报告：

```text
demands/<demand-name>/rd/output/<session-id>/
```

机器状态：

```text
.speclane/demands/<demand-name>/
```

## 边界

- RD 可以修改业务代码。
- RD 标准产物必须由脚本生成。
- RD 不直接维护 QA 测试报告，verify 只负责研发自验证收口。
