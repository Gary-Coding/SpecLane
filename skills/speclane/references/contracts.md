# 工作流契约

## 输入来源

- `workflow_source=todo`：直接读取 `todo_file`
- `workflow_source=openspec`：从 OpenSpec `tasks.md` 生成桥接 `todo_file`，并写入 `openspec-bridge-context.json`

## 关键产物

- `discovery.json`：代码定位证据
- `plan.json`：工程计划与验收基线
- `review.json`：结构化审查结论
- `verify.json`：结构化验证结论
- `execution-summary.json`：写回 OpenSpec change 的执行摘要
- `archive-input.json`：归档前检查输入
- `archive-result.json`：归档执行结果

## 可靠性边界

- `plan.json` 是实现阶段的执行基线
- `review.json` 和 `verify.json` 是是否允许归档的核心证据
- `execution-summary.json` 是 OpenSpec change 的共享摘要，不是长期规格 source of truth
- `openspec/specs/` 在 archive 完成后才成为新的长期规格基线
- `archive-input.json.merge_mode` 只要不是 `safe_merge`，就不允许自动 archive

## 归档顺序

1. `openspec-writeback`
2. `openspec-archive-check`
3. `openspec-archive`

## 归档前置条件

- `review.result == passed`
- `verify.result == 通过`
- `status.phase == done`
- `archive-input.json.archive_ready == true`
- `archive-input.json.spec_conflicts` 为空
- `archive-input.json.merge_mode == safe_merge`
