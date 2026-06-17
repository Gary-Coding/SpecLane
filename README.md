# spectrace

Structured AI Delivery Workflow

## 它是什么

`spectrace` 是一个面向 AI 编码的工程交付工作流 skill。它把一次需求交付拆成规格、计划、实现、自查、审查、验证和归档，让 AI 不再零散写代码，而是按可追踪、可验证、可复盘的流程工作。

English: SpecTrace turns requirements and specs into traceable AI-delivered software changes.

它支持两种入口：

- `todo` 模式：直接用 `todo.md` 驱动交付，适合轻量需求。
- `openspec` 模式：先生成 OpenSpec change，再桥接成 `todo.md`，适合正式需求迭代。

## 适合谁

- 使用 Claude Code、Codex 等 AI 编码工具的开发者。
- 需要在存量系统、多仓库、微服务项目上持续迭代的团队。
- 希望 AI 编码过程有计划、审查、验证、通知和归档记录的团队。
- 已经使用或准备使用 OpenSpec 沉淀需求规格的团队。

## 三步开始

```bash
npm install -g @gary-coding/spectrace@latest
spectrace init
spectrace sync --target both
```

然后在 AI 中发送工作流命令。

OpenSpec 模式：

```text
/st:propose add-phone-filter
/st:bridge
# 人工审核 todo.md 后
/st:apply
```

todo 模式：

```text
/st:apply
```

更多命令见 [docs/st命令协议.md](docs/st命令协议.md)。

## 一个最小示例

需求：给用户列表增加手机号精确筛选。

1. 初始化工作区：

```bash
spectrace init
```

2. 把需求写入初始化生成的需求文件，或维护好 `todo.md`。

3. 使用 OpenSpec 模式时，在 AI 中输入：

```text
/st:propose add-user-phone-filter
```

生成规格后继续：

```text
/st:bridge
```

审核 `todo.md` 后：

```text
/st:apply
```

AI 会按当前工作区配置推进计划、实现、自查、审查、验证，并在 OpenSpec 模式下回写执行摘要和归档检查结果。

## 工作空间配置

每个业务工作空间都需要有 `workspace.yml`。

最小 `todo` 模式示例：

```yaml
version: 1
mode: manual
workflow_source: todo
todo_file: todo.md
reference_files: []
code_path: ../../../code
output_dir: output
```

如果自动识别出的验证命令不适合当前项目，可以在 `workspace.yml` 中覆盖：

```yaml
verify_commands:
  default: pnpm test && pnpm build
  frontend-app: pnpm test && pnpm build
  user-service: go test ./...
```

最小 `openspec` 模式示例：

```yaml
version: 1
mode: manual
workflow_source: openspec
vars:
  demand_name: add-phone-filter
demand_file: demands/${demand_name}/需求.md
todo_file: demands/${demand_name}/todo.md
reference_files: []
code_path: ../../../code
output_dir: demands/${demand_name}/output
openspec:
  changes_dir: ../openspec/changes
```

`demand_file` 可以是本地 Markdown，也可以是飞书/Lark 云文档 URL。使用云文档时需要先安装并授权官方 CLI：

```bash
npx @larksuite/cli@latest install
lark-cli config init --new
lark-cli auth login --recommend
```

如果同一个工作空间经常切换需求，可以用 `vars` 避免重复修改路径：

```yaml
version: 1
mode: auto
workflow_source: openspec
vars:
  demand_name: 7-deamnd-addition-rate
demand_file: demands/${demand_name}/需求.md
todo_file: demands/${demand_name}/todo.md
reference_files:
  - ../docs/需求分析与实现指南.md
code_path: ../../../code
output_dir: demands/${demand_name}/output
openspec:
  changes_dir: ../openspec/changes
```

OpenSpec change 名称不从 `demand_name` 推导。请在 `/st:propose <change-name>` 后显式指定，例如 `/st:propose demand-addition-rate`。后续 `/st:bridge`、`/st:apply` 会使用 propose 阶段记录的当前 change。

skill 自身配置位于：

```text
~/.spectrace/skill-config.yml
```

如果该文件不存在，首次初始化时会自动生成默认配置并暂停流程，等待补全。

## 运行时产物

给机器读取的会话产物：

```text
<workspace>/.spectrace/current-session.json
<workspace>/.spectrace/sessions/<session_id>/discovery.json
<workspace>/.spectrace/sessions/<session_id>/plan.json
<workspace>/.spectrace/sessions/<session_id>/self-check.json
<workspace>/.spectrace/sessions/<session_id>/review.json
<workspace>/.spectrace/sessions/<session_id>/verify.json
<workspace>/.spectrace/sessions/<session_id>/status.json
```

给人查看的报告：

```text
<output_dir>/<session_id>/discovery.md
<output_dir>/<session_id>/plan.md
<output_dir>/<session_id>/self-check.md
<output_dir>/<session_id>/review.md
<output_dir>/<session_id>/verify.md
```

OpenSpec 模式额外产物：

```text
<workspace>/.spectrace/openspec-bridge-context.json
<change_dir>/spectrace/execution-summary.json
<change_dir>/spectrace/archive-input.json
<change_dir>/spectrace/archive-result.json
```

## 文档入口

- [docs/st命令协议.md](docs/st命令协议.md)
- [docs/中文使用手册.md](docs/中文使用手册.md)
- [docs/English User Guide.md](docs/English%20User%20Guide.md)
- [docs/项目架构与设计说明.md](docs/项目架构与设计说明.md)
- [skills/spectrace-rd/SKILL.md](skills/spectrace-rd/SKILL.md)
- [skills/spectrace-rd/references/workflow.md](skills/spectrace-rd/references/workflow.md)
- [skills/spectrace-rd/references/contracts.md](skills/spectrace-rd/references/contracts.md)

## 许可证

本项目使用 [MIT License](LICENSE)。
