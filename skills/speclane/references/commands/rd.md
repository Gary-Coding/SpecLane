# RD 命令协议

RD 是当前可执行阶段，现有 `/sl:*` 主命令均归属于 RD 交付链路。

## 当前命令

```text
/sl:propose <change-name>
/sl:bridge
/sl:plan
/sl:apply
/sl:review
/sl:verify
/sl:archive-check
/sl:archive
/sl:status
/sl:recover
```

## 执行入口

始终通过脚本路由：

```bash
python3 scripts/run-workflow.py route-check --command-text "<command>"
python3 scripts/run-workflow.py route-sl --command-text "<command>"
```

## 边界

- `/sl:propose` 和 `/sl:bridge` 不改代码。
- `/sl:apply` 才进入代码实现，并要求补充单元测试。
- `finish-implement` 会先执行单元测试并生成 `unit-test.json/md`，再进入 self-check。
- 标准 JSON 和 Markdown 报告只能由脚本生成。
- 验证和通知只能通过 verify 链路收口。
