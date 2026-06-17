# 工作流契约

## 配置文件

工作流会读取两份配置：

- `<workspace>/workspace.yml`
- `~/.spectrace/skill-config.yml`

`<workspace>` 就是当前使用这个 skill 的目录。

`workspace.yml` 必须包含：

- `version`
- `mode`
- `workflow_source`
- `todo_file`
- `demand_file`
- `reference_files`
- `code_path`
- `output_dir`

其中 `demand_file` 可选，主要作为 `/st:propose` 的原始需求输入。以上路径可以使用相对路径或绝对路径。相对路径按当前工作空间根目录解析。

`workspace.yml` 是用户维护的工作空间契约。AI 只能读取和校验该文件，禁止自动编辑、重写或格式化它。如果配置需要调整，AI 必须停止并说明需要用户修改的字段。

`workspace.yml` 支持可选 `verify_commands`。当项目自动识别出的验证命令不准确，或团队希望固定验证命令时，可以配置：

```yaml
verify_commands:
  default: pnpm test && pnpm build
  frontend-app: pnpm test && pnpm build
  user-service: go test ./...
```

key 可以是 `default`、目标仓库目录名或目标仓库绝对路径。匹配优先级是绝对路径、目录名、`default`。

`workspace.yml` 支持可选 `vars`：

```yaml
vars:
  demand_name: 7-deamnd-addition-rate
```

路径字段可以通过 `${demand_name}` 或 `${vars.demand_name}` 引用变量。

`~/.spectrace/skill-config.yml` 可以包含可选通知：

- `notification.pushplus.token`
- `notification.pushplus.ordinary.enabled`
- `notification.pushplus.ordinary.channel`
- `notification.pushplus.ordinary.template`
- `notification.feishu.enabled`
- `notification.feishu.webhook_url`
- `notification.feishu.secret`

如果 `~/.spectrace/skill-config.yml` 不存在：

- 首次运行时自动创建
- 创建后当前工作流立即停止
- 用户完善配置后再重新执行

通知规则：

- `ordinary.enabled=true` 时发送普通 PushPlus 消息，默认发给自己
- `feishu.enabled=true` 时通过飞书原生自定义机器人 webhook 发送消息
- `feishu.secret` 可选，仅在机器人开启签名校验时填写
- 两条路由可以同时开启，工作流结束后会分别发送

`workflow_source` 支持：

- `todo`
- `openspec`

OpenSpec 模式下还需要：

- `openspec.changes_dir`
- 可选 `openspec.tasks_file`
- 可选 `openspec.proposal_file`
- 可选 `openspec.design_file`
- 可选 `openspec.specs_dir`
- 可选 `openspec.writeback_dir`

OpenSpec change 名称必须通过 `/st:propose <change-name>` 显式指定。工作流不会从 `vars.demand_name`、需求文件名或需求标题推导 change 名称。`openspec.change_dir` / `openspec.change_name` 只作为兼容旧配置的字段，不建议新增配置。

`todo_file` 在两种模式下含义不同：

- `todo`：用户直接维护
- `openspec`：由 OpenSpec `tasks.md` 桥接生成，作为执行入口

`demand_file` 是原始需求文件：

- `openspec` 模式下，`/st:propose` 优先读取它生成或完善 change
- 可以配置为本地 Markdown 路径，也可以配置为飞书/Lark 云文档 URL
- 飞书/Lark 云文档 URL 通过官方 `lark-cli docs +fetch` 读取；未安装时按提示执行 `npx @larksuite/cli@latest install`、`lark-cli config init --new`、`lark-cli auth login --recommend`
- `reference_files` 是技术参考资料，不应该用来猜测哪个文件是原始需求
- `openspec` 模式下，`/st:propose` 必须读取真实存在的 `reference_files`，并把内容写入 `propose-input.json` / `propose-input.md`，作为生成 `proposal.md`、`design.md`、`tasks.md` 的上下文

`code_path` 可以是：

- 单个项目仓库根目录
- 包含多个服务仓库的聚合目录

如果是聚合目录，工作流应优先根据 todo 中的服务名约束自动定位目标仓库。

如果 todo 中明确指定了多个服务，工作流应解析出多个目标仓库，并在后续阶段逐仓执行。

实施阶段会尝试识别主流工程类型并推断验证命令：

- Java：Maven / Gradle
- Node.js / 前端：npm / pnpm / yarn / bun，包含 Vue、React、Next、Nuxt、Svelte、Angular 等常见框架
- Go：`go test ./...`
- Python：pytest / unittest，支持 uv / Poetry 前缀
- Rust：Cargo
- .NET：dotnet
- PHP：Composer / PHPUnit
- Ruby：Bundler / RSpec / Rake
- Make / CMake：Makefile 或已有 build 目录下的 CTest

如果无法识别可靠验证命令，verify 阶段必须进入 blocked，而不是伪造通过结果。

## 运行时目录布局

工作空间内部只保存给 AI 使用的数据：

- `<workspace>/.spectrace/current-session.json`
- `<workspace>/.spectrace/st-state.json`
- `<workspace>/.spectrace/sessions/<session_id>/discovery.json`
- `<workspace>/.spectrace/sessions/<session_id>/plan.json`
- `<workspace>/.spectrace/sessions/<session_id>/self-check.json`
- `<workspace>/.spectrace/sessions/<session_id>/review.json`
- `<workspace>/.spectrace/sessions/<session_id>/verify.json`
- `<workspace>/.spectrace/sessions/<session_id>/status.json`

给人查看的 Markdown 产物统一写到输出目录：

- `<output_dir>/<session_id>/discovery.md`
- `<output_dir>/<session_id>/plan.md`
- `<output_dir>/<session_id>/self-check.md`
- `<output_dir>/<session_id>/review.md`
- `<output_dir>/<session_id>/verify.md`

会话附加产物：

- `<workspace>/.spectrace/sessions/<session_id>/notification.json`

通知验收规则：

- `notification.json` 是唯一通知证据
- `status.json.notification_status` 只是摘要，不能单独作为通知成功依据
- 通知只能由 `run-workflow.py verify` 调用 `run-verify-and-report.py` 和 `common.notify_workflow_result()` 发送
- AI 禁止直接调用飞书 webhook，禁止手工拼接飞书卡片 JSON
- 启用飞书时，`notification.json` 必须包含 `source=run-workflow.py verify`、fingerprint 匹配、`route=feishu`、`template=interactive`、`status=sent` 的结果，才算飞书通知成功

## 会话规则

- OpenSpec 模式下，`st-state.json` 是脚本状态机，记录 `phase`、`allowed_next`、`current_change`、`last_command` 和关键产物路径
- `/st:propose` 后状态为 `proposed`，只允许 `/st:bridge`
- `/st:bridge` 后状态为 `bridged`，允许 `/st:apply` 或 `/st:plan`
- `/st:plan` 后状态为 `planned`，只允许 `/st:apply`
- `/st:verify` 通过后状态为 `verified`，只允许 `/st:archive-check`
- `/st:archive-check` 通过后状态为 `archive_ready`，只允许 `/st:archive`
- 所有阶段推进必须先通过 `run-workflow.py validate-state <command>` 等价校验，不能只依赖 AI 回复
- `plan` 只有在没有有效计划会话、当前会话已完成/归档/阻塞，或当前会话失效时才创建新的 `session_id`
- 如果当前 session 已有 `plan.json` 且仍停留在计划确认阶段，重复执行 `plan` 必须复用当前 session，不能创建新 session
- 如果当前 session 已进入 `implement`、`self_check`、`review`、`verify` 等交付阶段，重复执行 `plan` 必须被脚本拒绝
- 新会话不能覆盖历史会话目录
- `current-session.json` 只指向当前正在推进的会话
- 后续 `start-implement`、`finish-implement`、`review`、`verify`、`status` 都基于当前会话执行
- `plan` 会自动执行 `discover`，`finish-implement` 会自动执行 `self-check`
- `workflow_source=openspec` 时，只有用户显式执行 `/st:bridge` 才会桥接 OpenSpec `tasks.md` 到 `todo_file`
- `workflow_source=openspec` 时，`init` / `plan` 只校验已有桥接 todo，不能自动创建或重写 `todo_file`
- `workflow_source=openspec` 时，`propose-openspec` 优先调用 OpenSpec CLI 创建 change、读取 status 和 artifact instructions
- `workflow_source=openspec` 时，`review` / `verify` 会自动把执行摘要写回 `openspec.writeback_dir`
- OpenSpec 长期规格归档建议显式执行 `prepare-archive-openspec` 与 `archive-openspec`；归档检查会结合 OpenSpec CLI status 与 spectrace 的 spec baseline 冲突检测
- `prepare-archive-openspec` 会检测 spec baseline 是否发生变化；只有 `merge_mode=safe_merge` 才允许后续归档
- `auto` 模式下，除非进入硬阻塞，否则不能在对话里要求用户批准继续
- 工作流总耗时按当前会话开始到 verify 收口结束的真实墙钟时间计算
- AI 禁止编辑 `<workspace>/workspace.yml`
- AI 禁止直接写 `.spectrace/current-session.json`、`.spectrace/sessions/**/status.json`、`plan.json`、`review.json`、`verify.json`、`notification.json` 和 output 下的标准 Markdown 报告；这些标准产物只能由脚本生成

## 硬阻塞定义

只有出现以下情况，才允许停止并等待用户：

- 工作空间配置缺失或不合法
- todo 文件缺失，且无法自动创建或内容为空到无法判断需求
- 无法定位目标仓库
- 多仓场景下目标服务不明确
- 必要命令无法执行，且无法自动兜底
- 验证失败到必须人工介入
- 实现自查或 review 发现阻塞级问题
- 发现需求与代码现实严重冲突，继续修改会明显越界

以下情况不属于硬阻塞，必须继续推进：

- 计划还不够精确
- 还需要先去代码里定位具体实现位置
- 想先让用户确认某一步是否继续
- review 过程中发现计划要补充

## 产物规则

- JSON 产物保持稳定、结构化、便于机器读取
- Markdown 产物保持简洁、便于人阅读
- 只要阶段、阻塞、下一步动作发生变化，就更新当前会话的 `status.json`
- 必须通过 `scripts/run-workflow.py` 推进阶段，避免手工拼接状态
- verify 收口后，如果配置了通知，自动发送工作流完成通知，但通知失败不能覆盖真实验证结论
- 工作流完成通知只能通过 `run-workflow.py verify` 发送，禁止 AI 直接调用飞书 webhook，禁止 AI 手工拼接飞书卡片 JSON
- 如果 session 已经被标记为 `done`，但缺少 `verify.json`、`notification.json` 或输出目录下的 Markdown 报告，说明前一次没有走标准收口，应通过 `/st:verify` 重新执行标准验证收口
