# Production Code Skill · 生产级代码工程

**让 AI 按项目实际语言、框架和运行模型编写、修复、重构与审查代码。**

A reusable Agent Skill for production-quality coding and code review, with deep Python and Java guidance and practical adaptations for other languages.

[核心 Skill](production-code/SKILL.md) · [安装与使用](docs/usage.zh-CN.md) · [验证报告](docs/validation.md) · [参与改进](CONTRIBUTING.md) · [MIT](LICENSE)

## 快速开始

克隆仓库：

```sh
git clone https://github.com/quyanxin0629-del/production-code-skill.git
cd production-code-skill
```

在 Codex 中个人使用：把完整 `production-code` 文件夹复制到 `~/.agents/skills/`。

```sh
mkdir -p "$HOME/.agents/skills"
cp -R ./production-code "$HOME/.agents/skills/"
```

团队使用：把该文件夹放入目标项目的 `.agents/skills/production-code/`，随仓库维护。已有同名目录时先比对合并；保持主文件和 references 一起安装。其他兼容 Agent Skills 的工具按其产品说明导入完整目录。

显式调用：

```text
使用 $production-code 完成这个需求。
先确认项目实际语言、版本和现有约定，完成代码与必要验证，
最后说明行为变化和实际检查结果。
```

只读审查：

```text
使用 $production-code 审查这次变更，不修改代码。
只报告有具体触发条件、影响和代码位置的问题。
```

Skill 也可根据任务自动匹配。Codex 通常会自动发现变更，未显示时可重启客户端；安装位置与调用方式见 [OpenAI 官方文档](https://learn.chatgpt.com/docs/build-skills)。

## 覆盖八个工程维度

| 维度 | 核心内容 | 参考 |
|---|---|---|
| 1. 可读性与建模 | 卫语句、表驱动、策略、状态机、纯函数、参数与返回契约 | [核心规则](production-code/references/core.md) |
| 2. 语言用法与生命周期 | 集合复杂度、惰性处理、资源作用域、装饰器与缓存边界 | [核心规则](production-code/references/core.md)、语言指南 |
| 3. 防御性编程 | 金额精度、分摊守恒、溢出、UTC / DST、JSON、边界校验 | [完整章节](production-code/references/defensive-and-observability.md) |
| 4. 异常与可观测性 | 异常分类、因果链、取消、TraceID、MDC、日志成本与脱敏 | [完整章节](production-code/references/defensive-and-observability.md) |
| 5. 并发与流量治理 | 有界任务 / 队列、线程池、复合原子操作、分段锁、死锁与取消 | [Python](production-code/references/python.md)、[Java](production-code/references/java.md)、[JUC](production-code/references/juc-locks.md) |
| 6. MySQL 与 ORM | 事务、CAS、锁范围、N+1、稳定游标分页、分批写入 | [数据与韧性](production-code/references/data-and-resilience.md) |
| 7. Redis 与分布式协调 | BigKey、扫描、缓存失效、Pipeline、Lua、租约与 fencing | [数据与韧性](production-code/references/data-and-resilience.md) |
| 8. 外部依赖与演进 | 超时预算、抖动重试、幂等、Outbox / Inbox、MQ、滚动迁移 | [数据与韧性](production-code/references/data-and-resilience.md) |

## 语言与框架

- **Python 深度指南**：FastAPI、asyncio、Pydantic 2、SQLAlchemy 2.x、HTTPX。
- **Java 深度指南**：Spring MVC / WebFlux、事务与 AOP、JUC、CompletableFuture、虚拟线程、MyBatis。
- **其他语言适配**：[TypeScript / JavaScript、React、Go、Rust / Tokio、C# / .NET、C / C++](production-code/references/other-languages.md)。覆盖执行模型与关键失误模式，不宣称与 Python / Java 同等深度。
- **未知语言与场景**：先识别版本、运行时、所有权、错误模型和资源边界，再映射工程不变量。前端、CLI、嵌入式不会被强行套上数据库与微服务架构。

涉及外部 URL、上传 / 归档、进程执行、反序列化或 HTML 输出时，按需读取 [安全边界](production-code/references/security-boundaries.md)。数据库读取默认按需投影与限页，关联批量加载；比较扫描行、返回字节、往返与一致性，不以查询次数单独判断优劣。

## 它怎样工作

先确认项目事实，再读取相关参考，按任务在五种模式中执行：**实现 / 修复、重构、只读审查、方案设计、讲解**。核心入口保持精简，详细模式按需加载。

关键判断落到业务条件、失败时序、资源所有权和实际证据。参考包含 Bad / Good、底层机制与适用边界；日常编码交付保持简洁，不输出整本教材。

它强调：

- 并发容器不等于复合操作原子，进程锁不等于跨实例一致性。
- Future 超时、任务取消不证明线程或远端副作用已经停止。
- Stream / yield 不证明端到端固定内存，队列与输入输出必须一起受控。
- JSON / 模型校验不替代授权、业务状态与数据库约束。
- 索引、TTL、批次、重试和线程数按负载与版本判断，示例数字不当作统一标准。
- 只报告实际执行的检查，不伪造编译、性能或生产验证结论。

## 可复现验证

安装 Skill 不需要 Python 依赖。以下依赖仅用于维护者运行仓库验证：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/validate.py
```

Java 标准库机制验证需要 JDK 17+，无需 Maven 或外部服务：

```sh
python3 scripts/validate_java.py
```

Windows 可使用 `.venv\Scripts\python` 执行后两步。

验证器检查元数据、引用可达性、代码围栏和 Python AST，自动发现 tests 下的新增回归用例，直接提取参考中的代码运行。测试覆盖金额、时间、JSON、上下文、资源退出、有界批处理、指标故障、缓存边界、重试、消费幂等和安全端口；Lua 通过 Lupa 2.6 的 Lua 5.1 与 Redis 命令替身执行。另从 Java 参考提取锁、快照与 Future 示例编译运行。最新结果见 [验证报告](docs/validation.md)。

GitHub Actions 配置覆盖 Linux / Windows 上的 Python 3.11 / 3.12，以及 Linux 上的 JDK 17 / 21。业务端口、实体和 SDK 仍需接入项目；机制测试不等同于 Spring、真实 HTTP、MySQL、Redis 或 Kafka 集成及生产压测。

## 项目结构

```text
production-code/       可安装的 Skill，含 SKILL.md、agents 和 references
docs/                  使用说明与验证范围
scripts/validate.py    可复现校验入口
scripts/validate_java.py  Java 参考编译与机制验证
tests/                 批处理边界与取消测试
.github/workflows/     CI 验证
```

参考资料以语言、框架与中间件的一手官方文档为主，链接保留在相关章节中；按目标项目实际版本重新核对变化行为。欢迎提交带触发条件、最小复现和验证证据的改进。

## 许可证

[MIT License](LICENSE) · Copyright © 2026 Cyrix
