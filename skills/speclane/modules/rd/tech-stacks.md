# 技术栈识别

RD 阶段根据项目根目录文件和代码结构识别技术栈，并推断默认验证命令。显式配置 `workspace.yml.verify_commands` 时，以配置为准。

## Java / Spring

识别规则：

- 存在 `pom.xml` 或 `mvnw`：Maven 项目。
- 存在 `build.gradle`、`build.gradle.kts` 或 `gradlew`：Gradle 项目。
- 源码里出现 `@SpringBootApplication`：Spring Boot 项目。

默认命令：

- Maven：`./mvnw test` 或 `mvn test`
- Gradle：`./gradlew test` 或 `gradle test`

计划提示：

- 沿 Controller、请求 DTO、Service、Repository、测试链路梳理影响面。
- 关注非法输入、边界条件、兼容性和回归测试。
- 多服务需求要按仓库分别确认影响文件、测试命令和验证结果。

## Node.js / 前端

识别规则：

- 存在 `package.json`：Node.js 项目。
- 存在 `pnpm-lock.yaml`、`yarn.lock`、`bun.lockb` 或 `package-lock.json`：推断包管理器。
- 依赖中出现 Vue、React、Next、Nuxt、Svelte、Angular 时，按对应前端框架处理。

默认命令：

- 优先执行 `test`、`typecheck`、`lint`、`build` 中与本次改动相关的最小命令。
- 包管理器优先级按锁文件确定：pnpm、yarn、bun、npm。

## Go

识别规则：

- 存在 `go.mod`。

默认命令：

- `go test ./...`

## Python

识别规则：

- 存在 `pyproject.toml`、`requirements.txt`、`pytest.ini`、`tox.ini` 或 Python 包目录。
- 存在 `uv.lock` 或 Poetry 配置时，使用对应前缀。

默认命令：

- 优先 `python -m pytest`
- 否则 `python -m unittest discover`

## 其他技术栈

- Rust：存在 `Cargo.toml`，默认 `cargo test`
- .NET：存在 `.csproj`、`.sln`，默认 `dotnet test`
- PHP：存在 `composer.json`，默认 `composer test` 或 `vendor/bin/phpunit`
- Ruby：存在 `Gemfile`，默认 `bundle exec rspec` 或 `bundle exec rake test`
- Make：存在 `Makefile`，优先 `make test`，否则 `make`
- CMake：存在 `CMakeLists.txt`，已有 `build` 目录时可用 `ctest --test-dir build`

## 识别失败

无法识别可靠验证命令时，verify 阶段必须进入 blocked，并说明需要用户补充 `workspace.yml.verify_commands`。
