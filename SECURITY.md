# Security Policy

## 安全边界

SpecTrace 会读取和写入本地工作区、Codex / Claude skill 目录以及配置的代码目录。使用前应确认当前工作区是可信目录。

## 不应提交或发布的内容

- `.spectrace/` 会话状态与执行产物
- `demands/` 需求中间产物
- `workspace.yml` 中的个人绝对路径
- webhook、token、cookie、账号密码
- `__pycache__/`、`.DS_Store` 等本地缓存

## 通知集成

飞书、PushPlus 等通知配置应放在本机 `~/.spectrace/skill-config.yml` 中，不应提交到仓库。

## 漏洞反馈

如果发现安全问题，请通过 GitHub issue 联系维护者，并避免在 issue 中公开敏感信息。
