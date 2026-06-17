# OpenClaw 接入说明

这个 skill 应保持平台无关，不要把运行逻辑绑死在某个 Agent 平台上。

面向未来 OpenClaw 接入时，遵守这些约束：

- 把 OpenClaw 视为调用方，而不是工作空间拥有者
- 所有运行时产物都保留在本地 Mac 节点的工作空间中
- 用当前会话的 `status.json` 作为持久化进度面板
- 把用户确认动作映射到 `pending_confirmation_for`
