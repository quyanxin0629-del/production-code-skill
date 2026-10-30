# production-code：生产级代码工程 Skill

这是可安装、可复制进团队仓库的完整 Skill。它将八个工程维度转化为 AI 的实际编码流程，按项目语言和风险读取参考，支持实现、修复、重构、只读审查、方案设计和讲解。

## 内容与覆盖深度

| 文件 | 内容 |
|---|---|
| production-code/SKILL.md | 入口、模式、项目探测、决策顺序和交付标准 |
| references/core.md | 维度一 / 二：可读性、策略、状态机、契约、集合、生命周期 |
| references/defensive-and-observability.md | 完整维度三 / 四：精度、时间、JSON、校验、异常、TraceID、日志 |
| references/python.md | Python、FastAPI、asyncio、Pydantic、SQLAlchemy；有界并发与取消 |
| references/java.md | Java、Spring、JUC、MyBatis；事务代理、线程池、Future 与游标 |
| references/juc-locks.md | 并发容器复合操作、分段锁、锁顺序、读写与不可变快照 |
| references/data-and-resilience.md | 维度六 / 七 / 八：MySQL、Redis、外部调用、MQ、兼容迁移 |
| references/other-languages.md | TS/JS、React、Go、Rust、C#、C/C++ 和未知语言适配 |
| references/verification.md | 风险相称的测试、并发 / 故障验证、性能证据 |
| references/security-boundaries.md | 外部 URL、文件 / 归档、进程调用、HTML 与不可信反序列化 |
| agents/openai.yaml | Codex 界面元数据，保持默认自动匹配能力 |

Python 和 Java 提供深度参考；其他语言提供执行模型、关键风险和正反例，并要求按项目版本核对 API。它不声称穷尽所有语言和业务。

## 安装

保留完整 production-code 目录，不能只复制 SKILL.md。该目录不依赖其他自定义 Skill、私有账号、特定插件或附加执行脚本。

团队使用：将整个目录放入目标仓库的 `.agents/skills/production-code/`，随仓库评审与维护。不要直接覆盖已有同名 Skill，应先比对合并。

个人 Codex 使用：在仓库根目录中执行以下命令；有同名目录时先处理已有内容。

```sh
mkdir -p "$HOME/.agents/skills"
cp -R ./production-code "$HOME/.agents/skills/"
```

Codex 支持显式调用与根据 description 自动匹配；变更通常会自动发现，如界面未出现可重启客户端。当前位置与调用格式依据 [OpenAI 官方 Skill 文档](https://learn.chatgpt.com/docs/build-skills)。旧客户端或其他支持 Agent Skills 的工具应按其实际导入位置使用，避免在多个扫描位置放置重复同名副本。

## 直接使用

```text
使用 $production-code，实现这个需求。先确认项目实际语言、版本和现有约定，
完成必要代码与验证，最后简要说明行为变化和实际检查结果。
```

```text
使用 $production-code，审查这次变更。只读，不修改代码。
仅报告有具体触发条件、影响和代码位置的问题，不按维度凑数量。
```

```text
使用 $production-code，重构这个 Java 服务，保持公开行为兼容。
重点检查 Spring 事务边界、幂等、线程池容量和取消，不升级框架。
```

```text
使用 $production-code，修复这个 Python 异步批处理。
要求队列与任务数有界、超时及取消有效、失败时回收子任务。
```

```text
使用 $production-code，修改这个 React 表单。
沿用现有组件和数据请求方案，覆盖加载、失败、键盘操作和重复提交。
```

## 团队落地

- 在项目 AGENTS.md 中保留实际构建命令、版本、目录边界和团队特例；Skill 负责可复用的工程决策。
- 按需读取参考，日常代码交付保持简洁；只有讲解模式才展开 Bad / Good 和机制。
- 对金额、权限、并发和数据协议选择有意义的验证；低影响改动不强制补无价值测试。
- 示例阈值、TTL、批次和容量需按业务确认；不要直接当作统一生产配置。
- Skill 不替代编译、集成环境、性能测量和人工审查；报告只声称实际取得的证据。

本包的校验结果与验证边界见[验证报告](validation.md)。参考来源核对日期为 2026-10-30；后续使用按仓库实际版本重新确认变化行为。
