# Contributing

## 开发环境

需要：

- Node.js 18+
- npm 9+
- Python 3.10+

## 本地检查

```bash
npm run check
npm run smoke
npm run pack:check
node bin/speclane.js --help
node bin/speclane.js doctor --workspace .
```

## 修改原则

- `skills/speclane/SKILL.md` 是唯一 skill 入口，修改命令语义时必须同步更新。
- `skills/speclane/references/commands/` 是 `/sl:*` 协议说明，修改命令流程时必须同步更新。
- `skills/speclane/scripts/` 下脚本是受控执行入口，不要让 AI 通过手写状态文件绕过流程。
- 发布前必须检查 npm 包内容，确认没有本地工作区产物、缓存文件、密钥或个人路径。

## 发布流程

1. 更新 `package.json.version`。
2. 更新 `CHANGELOG.md`。
3. 执行 `npm run check`。
4. 执行 `npm run pack:check`。
5. 执行 `npm publish --access public`。
