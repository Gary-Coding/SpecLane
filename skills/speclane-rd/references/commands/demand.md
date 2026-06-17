# `/sl:demand`

用途：管理同一工作区下的多个需求实例，让每个需求拥有独立配置、状态、session、锁和 output。

## 支持命令

```text
/sl:demand new <demand-name>
/sl:demand use <demand-name>
/sl:demand list
/sl:demand status <demand-name>
```

主流程命令也可以显式指定需求：

```text
/sl:propose <change-name> --demand <demand-name>
/sl:bridge --demand <demand-name>
/sl:plan --demand <demand-name>
/sl:apply --demand <demand-name>
/sl:status --demand <demand-name>
/sl:recover --demand <demand-name>
```

## 产物

- `.speclane/active-demand.yml`
- `.speclane/demands/<demand-name>/demand.yml`
- `.speclane/demands/<demand-name>/current-session.json`
- `.speclane/demands/<demand-name>/todo-state.json` 或 `sl-state.json`
- `.speclane/demands/<demand-name>/sessions/<session_id>/`
- `demands/<demand-name>/todo.md`
- `demands/<demand-name>/output/<session_id>/`

## 约束

- 需求实例配置使用 YAML：`.speclane/demands/<demand-name>/demand.yml`。
- 同一需求内部仍然串行执行，由该需求自己的 `workflow.lock` 保护。
- 不同需求可以交错推进，但 AI 每次只能处理一个明确 demand。
- 未显式 `--demand` 时，脚本优先使用 `.speclane/active-demand.yml`。
- 没有 active demand 且没有显式 `--demand` 时，保持旧单需求工作区行为。

最终回复只说明当前 demand、动作结果、配置路径和允许的下一步。
