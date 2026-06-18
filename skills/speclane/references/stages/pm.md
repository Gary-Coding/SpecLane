# PM 阶段

PM 阶段面向需求梳理，目标是把原始想法整理成可评审、可交接的需求输入。

## 当前状态

当前阶段为协议预留，默认不执行脚本，不修改代码。

## 输入

- `demands/<demand-name>/input/需求.md`
- `demands/<demand-name>/input/references/`
- `workspace.yml.reference_files`

## 输出

推荐输出到：

```text
demands/<demand-name>/pm/
```

可包含：

- `requirement-brief.md`
- `acceptance-criteria.md`
- `scope.md`
- `open-questions.md`

## 边界

- PM 阶段不生成代码。
- PM 阶段不生成 RD 的 `todo.md`。
- 进入 RD 前，需求必须能被 `/sl:propose <change-name>` 或 todo 模式消费。
