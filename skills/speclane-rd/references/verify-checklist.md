# 验证清单

验证至少要回答这些问题：

- 选中的构建或测试命令是否通过
- 实际执行的命令是什么
- 命令在哪个仓库、哪个目录执行
- 命令耗时、退出码、失败摘要是什么
- 是否确实需要启动服务
- 是否还有需要用户手工执行的检查项

建议优先级：

- 优先执行最小相关测试命令
- 其次执行构建或全量测试命令
- 只有确实需要时才执行启动命令
- 输出简洁摘要，不要直接堆原始日志

验证矩阵建议包含：

- static：lint、format、typecheck
- unit：单元测试
- integration：模块或集成测试
- build：编译或打包
- smoke：人工接口或页面验证

当前无法自动识别命令时，应写明缺口，并把工作流置为 blocked。

## 主流项目自动识别

工作流会优先根据项目根目录文件推断验证命令：

- Java Maven：`./mvnw test` 或 `mvn test`
- Java Gradle：`./gradlew test` 或 `gradle test`
- Node.js / Vue / React / Next / Nuxt：根据 `package.json` scripts 和锁文件推断 `npm`、`pnpm`、`yarn` 或 `bun` 命令
- Go：`go test ./...`
- Python：优先 `python -m pytest`，否则 `python -m unittest discover`；uv / Poetry 项目会加对应前缀
- Rust：`cargo test`
- .NET：`dotnet test`
- PHP：`composer test` 或 `vendor/bin/phpunit`
- Ruby：`bundle exec rspec` 或 `bundle exec rake test`
- Make：优先 `make test`，否则 `make`
- CMake：已有 `build` 目录时使用 `ctest --test-dir build`

如果自动推断不适合当前团队，应在 `workspace.yml.verify_commands` 中覆盖。覆盖命令优先于自动识别结果。
