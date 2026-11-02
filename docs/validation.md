# production-code Skill 验证报告

最终复验日期：2026-11-02。本轮对完整演进工作区执行交付前验证与 Git 归档检查；Skill 入口保持 103 行，11 份参考内容未变。参考不是默认加载清单：先检索目录 / 标题，首轮精读最相关的 1～2 份或小节，按真实依赖补充。

## 复现方式

安装 requirements-dev.txt 后，在仓库根目录执行：

```sh
python scripts/validate.py
python3 scripts/validate_java.py
python3 scripts/validate_node.py
git diff --check
```

Node 机制验证需要 Node ≥20.3.0，无 npm 依赖，现已纳入 GitHub Actions 的 Node 20 / 22 矩阵。

本轮按上述顺序执行全部本地命令，退出码均为 0；Python 主验证复用已安装维护依赖的虚拟环境。

三个脚本也支持从其他工作目录用绝对脚本路径运行。Python 依赖仅用于维护测试，不是安装 Skill 的运行依赖。Java 需要 JDK 17+，以 --release 17 和 UTF-8 编译；Node 使用原生 test runner。生成源码 / 模块均放在 TemporaryDirectory，退出清理。缺少 Java 或 Node 时对应命令明确 NOT RUN 并非零退出，不把未执行算成功。

## 本轮实际结果

| 检查 | 实际结果与范围 |
|---|---|
| skill-creator quick_validate.py | 通过：frontmatter、名称与脚手架检查 |
| Python 统一验证 | 74 项通过；新增 1 项目录结构回归，保留此前 73 项检查 |
| Markdown / AST / 链接 | 11 份参考均从入口可达，本地链接有效；96 段 Python 代码通过 AST 解析；30 个显式示例 ID 无重复 |
| 目录守门 | 动态检查 references 下所有 Markdown：独立 `## 目录` 位于首个正文章节标题之前；围栏与 HTML 注释中的伪标题不计入，报错定位到具体参考文件 |
| Java 标准库机制 | 本地 Temurin 21.0.12.1 下 --release 17 编译成功，6 组机制测试通过 |
| Node 机制 | 本地 Node v26.9.0 下 6 项通过；直接提取 3 段真实 JavaScript 示例，无 npm 依赖 |
| Skill 实际试用（前轮） | 只读审查试用未将已校验 DTO 重复校验、固定 5 项非热列表查找报为缺陷；本轮未重复该试用 |
| diff | git diff --check 通过 |
| CI 配置 | Linux / Windows 的 Python 3.11 / 3.12、Linux JDK 17 / 21，以及新增的 Linux Node 20 / 22 矩阵；Node job 固定 checkout / setup-node commit SHA，不安装 npm 依赖。本轮仅本地验证，未运行远程 Actions |

Python 74 项由入口自带 12 项、批处理 10 项、数据边界 17 项、Python 示例 11 项、安全端口 4 项、验证器回归 11 项，以及签名 / 停机 9 项组成。Java 的 6 组和 Node 的 6 项分别报告，不与 Python 单测相加冒充覆盖率。AST 成功和显式 ID 不代表所有局部片段均可独立运行。

## 2026-10-31 目录守门修改

该轮仅修改以下 3 个文件，保留此前工作区改动；最终归档覆盖完整演进范围。

- scripts/validate.py：在 ArtifactTests.test_markdown_and_local_links 中检查所有参考文档的目录与章节顺序。可选首个 H1 作为文档标题；入口 SKILL.md 不强制目录。标题视图排除围栏与 HTML 注释，不改变示例解析输入。
- tests/test_validation_harness.py：新增 1 个测试方法，通过临时 Skill / reference 与真实 ArtifactTests 路径验证 7 个子用例：合法目录、带围栏示例的合法目录，以及缺失、错误级别、仅在围栏 / 注释中出现和位于正文之后的目录。7 个子用例不冒充 7 个独立测试。
- docs/validation.md：将 Python 总数更新为 74、验证器回归更新为 11，并记录守门范围。

先添加回归后运行旧验证器，确认缺失 / 错级 / 围栏伪标题 / 顺序错误均未被检出；接入断言后回归通过。独立复核发现的 HTML 注释伪标题也已加入同一回归，并在标题扫描中屏蔽。

## 前轮结构化演进的落点

1. **加载预算**：入口先目录 / 标题检索，优先 1～2 份 / 小节；跨层风险逐段补读，不能为预算漏掉必要证据。各参考顶部提供目录或检索词。
2. **防御分层**：API、跨信任边界、SDK / 公共组件维护完整契约；内部已校验 DTO 不逐层重复类型检查。数量合法不替代执行时授权、库存、状态和存储条件。计时饱和计数明确限定为组件后备诊断。
3. **审查契约**：P0 / P1 / P2 由可达性、影响与证据分级；单问题报告位置、触发 / 时序、影响和最小修复；不凑风格、假想输入或未经测量的非热路径优化。
4. **遗留演进与停机**：特征 / 黄金测试保护可观察行为；绞杀、影子对比与开关保留单一写入权威和可验证回退。停机先拒绝新工作，再在统一 deadline 内排空，最后按依赖顺序关闭。取消不等于撤销副作用或强杀线程。
5. **方言与安全边界**：PostgreSQL aborted 事务 / SAVEPOINT、缺失行锁与 ON CONFLICT 快照区别；签名使用专用秘密比较，租户在存储读写条件中绑定，资金 / 状态使用服务端权威判断。
6. **高频语言拆分**：TS / Node / React 与 Go 独立参考；覆盖 Promise 拒绝与所有权、SSR 隔离、Transport / Body、errgroup 派生 Context 的取消和等待。
7. **统一提取**：Python / Java / Lua / Node 共用 scripts/example_blocks.py，以围栏前的稳定 ID 读取，不按函数签名、正文子串或章节顺序查找；原行为断言保留。

## 提取契约与行为覆盖

- 标记采用独立一行 `<!-- example: stable-kebab-id -->`，与围栏间只允许空行；普通未标记片段仍受围栏 / AST 检查。
- 函数改名、章节重排、未标记诱饵不会改变 ID 选取；缺失、重复、语言漂移、悬空 / 分离标记、未闭合围栏会失败；跨文件重复 ID 也会失败。
- 解析保留反引号 / 波浪线、较长外层围栏、CRLF、Python 别名和围栏元数据；围栏内的字面标记不成为可执行 ID。
- 原有金额精度 / 分摊守恒、DST / 半小时偏移、JSON、上下文恢复、响应退出、指标故障、缓存边界、批处理背压 / 取消、重试预算、inbox ack 时点和 Lua 输入边界继续通过。
- 签名覆盖正文 / 密钥改变、首尾字节不匹配、公开协议长度，以及调用专用比较原语；不使用耗时微基准证明恒定时间。
- 停机覆盖顺序、截止超时、调用方取消，以及清理 finally 可超出 deadline 的真实协作取消边界；不提前关闭仍被活跃任务使用的数据库。
- Node 覆盖结果顺序、异步失败 / 同步抛错、取消与异步收尾、SSR 交错隔离及 AsyncLocalStorage 作用域恢复。

## 前轮结构化演进文件清单

| 组别 | 文件 |
|---|---|
| 入口 | production-code/SKILL.md |
| 通用规则 / 分层 / 审查 | production-code/references/core.md、defensive-and-observability.md、verification.md |
| 数据 / 安全 | production-code/references/data-and-resilience.md、security-boundaries.md |
| 语言 | production-code/references/python.md、java.md、juc-locks.md、other-languages.md；新增 typescript-node-react.md、go.md |
| 验证器 | scripts/validate.py、validate_java.py；新增 example_blocks.py、validate_node.py |
| 原回归迁移 | tests/test_batch.py、test_defensive_edges.py、test_python_examples.py、test_security_boundaries.py、test_validation_harness.py、tests/java/MechanismTests.java |
| 新行为与支持 | tests/test_evolution_boundaries.py、tests/example_support.py、tests/node/ownership.test.mjs |
| 配套文档 | README.md、CONTRIBUTING.md、docs/usage.zh-CN.md、docs/validation.md |

## 尚未证明的范围

没有连接真实 PostgreSQL、MySQL、Redis、Kafka 或支付服务；Lua 使用 Lupa 2.6 的 Lua 5.1 与 Redis 命令替身，事务 / HTTP / 停机均是端口级控制流验证，不证明真实隔离、网络释放、broker 重投、平台信号或故障转移。Spring、Kubernetes、PostgreSQL 的说明核对了链接中的官方资料，仍需目标版本集成测试。

本机没有 Go 工具链或 TypeScript 编译器，未做 Go / TSX 编译、React / 浏览器集成；Node 6 项不包含这些范围，也不验证真实 HTTP。其他语言片段未全部编译，不声称生产压测或全场景正确性。结构检查、示例机制、实际试用是不同层次的证据。
