# 工作区规范

SpecLane 使用一个主 skill 和一个 `workspace.yml` 管理多个需求。用户配置只写在 `workspace.yml`，`.speclane/` 只保存运行态。

## 标准目录

```text
<workspace>/
├── workspace.yml
├── docs/
├── .speclane/
│   ├── active-demand.yml
│   └── demands/<demand-name>/
└── demands/
    └── <demand-name>/
        ├── input/
        │   ├── 需求.md
        │   └── references/
        ├── pm/
        ├── spec/
        │   ├── openspec/
        │   │   ├── changes/
        │   │   └── specs/
        │   └── bridge/
        │       └── todo.md
        ├── rd/
        │   └── output/
        ├── qa/
        └── archive/
```

## 目录职责

- `input/`：原始需求、飞书文档导出、补充参考材料。
- `pm/`：产品梳理阶段产物，当前预留。
- `spec/openspec/changes/`：当前需求关联的 OpenSpec change。
- `spec/bridge/todo.md`：OpenSpec `tasks.md` 桥接后的研发交付入口。
- `rd/output/`：研发交付阶段的人可读报告。
- `qa/`：测试设计、测试执行和缺陷回归产物，当前预留。
- `archive/`：归档摘要、发布记录或团队沉淀产物。
- `.speclane/demands/<demand-name>/`：状态、session、锁、机器 JSON，禁止人工编辑。

## workspace.yml

推荐使用 `demands[]`，每个需求只写一次 `name`，路径用 `${demand_name}` 引用当前需求。

```yaml
version: 1
mode: auto
workflow_source: openspec
code_path: ../code
reference_files: []
openspec:
  changes_dir: demands/${demand_name}/spec/openspec/changes

demands:
  - name: 10-your-demand
    desc: 示例需求
    workflow_source: openspec
    mode: auto
    demand_file: demands/${demand_name}/input/需求.md
    todo_file: demands/${demand_name}/spec/bridge/todo.md
    output_dir: demands/${demand_name}/rd/output
    reference_files: []
    code_path: ../code
    openspec:
      changes_dir: demands/${demand_name}/spec/openspec/changes
```

AI 禁止编辑 `workspace.yml`。如果配置缺失或不合法，必须停止并说明需要用户修改的字段。

## 多需求选择

优先级：

1. 命令文本里的 `--demand <demand-name>`。
2. 环境变量 `SPECLANE_DEMAND_NAME`。
3. `.speclane/active-demand.yml`。
4. `workspace.yml.demands[]` 只有一个需求时自动选择。

多个需求同时存在且没有明确选择时，必须要求用户指定需求。
