# Java 识别与计划提示

针对 Java 工作区，优先使用这些识别规则：

- 存在 `pom.xml` 或 `mvnw` 时，按 Maven 项目处理
- 存在 `build.gradle`、`build.gradle.kts` 或 `gradlew` 时，按 Gradle 项目处理
- 源码里出现 `@SpringBootApplication` 时，可视为 Spring Boot 项目

优先推断的命令：

- Maven 测试：`./mvnw test` 或 `mvn test`
- Maven 启动：`./mvnw spring-boot:run` 或 `mvn spring-boot:run`
- Gradle 测试：`./gradlew test` 或 `gradle test`
- Gradle 启动：`./gradlew bootRun` 或 `gradle bootRun`

制定计划时建议：

- 沿着 controller、请求 DTO、service、repository、测试 这一条调用链梳理影响面
- 优先考虑非法输入、边界条件和回归测试覆盖
- 如果改动会收紧校验或改变返回行为，要显式指出兼容性风险
- 如果本轮需求同时涉及多个独立服务仓库，要按仓库分别确认影响文件、测试命令和验证结果
