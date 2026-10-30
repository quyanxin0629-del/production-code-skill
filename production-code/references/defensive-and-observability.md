# 维度三与维度四：防御性编程、异常与生产可观测性

这是完整执行参考。涉及相应边界时读取对应部分；不要把结算、权限或日志规则机械应用到没有该需求的纯计算或 UI 任务。Python 基线 3.11+ / Pydantic 2；Java 按仓库目标版本，Spring Boot 3+ 常用 jakarta 包。端口和领域类型需接入现有项目。

## 目录

- D1 金额来源、十进制、整数与舍入
- D2 分摊守恒、范围和溢出
- D3 时间点、营业日、时区、DST 与时钟
- D4 JSON 精度、未知字段、枚举与循环引用
- D5 入口验证、清洗与业务合法性的分界
- O1 异常分类与十条执行规则
- O2 因果链、错误边界与事务
- O3 请求上下文与全链路追踪
- O4 日志成本、敏感数据、级别与采样
- V 定向验证

# 维度三：防御性编程与魔鬼业务细节

## D1 金额精度从输入开始保护

Bad：先经过二进制浮点，再包装 Decimal / BigDecimal。

```python
from decimal import Decimal
amount = Decimal(19.99)
amount = Decimal(str(large_or_already_rounded_float))
```

```java
BigDecimal amount = new BigDecimal(19.99);
```

这会精确保留已经发生误差的值，或保留浮点转换后的文本，无法恢复原始输入。Decimal 也有精度上下文和舍入，不是任意算术都无限精确。

Good：先确定边界格式；本例契约为非负 CNY 文本、整数最多 12 位、最多 2 位小数、不允许指数和千位分隔符。限制是示例业务范围，不是全币种规则。

```python
import re
from decimal import Decimal, Inexact, InvalidOperation, localcontext

CNY_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,2})?")
CNY_QUANTUM = Decimal("0.01")

def parse_cny(raw: str) -> Decimal:
    if not isinstance(raw, str) or CNY_PATTERN.fullmatch(raw) is None:
        raise ValueError("invalid CNY amount")
    with localcontext() as context:
        context.prec = 32
        context.traps[Inexact] = True
        context.traps[InvalidOperation] = True
        return Decimal(raw).quantize(CNY_QUANTUM)
```

```java
// 与 Python 相同的输入契约；先限长，避免巨型数值输入。
static BigDecimal parseCny(String raw) {
    if (raw == null || raw.length() > 15 ||
        !raw.matches("(?:0|[1-9][0-9]{0,11})(?:\\.[0-9]{1,2})?")) {
        throw new IllegalArgumentException("invalid CNY amount");
    }
    return new BigDecimal(raw).setScale(2, RoundingMode.UNNECESSARY);
}
```

执行要求：

- 金额、费率、权重分别定义量纲和允许范围；不是所有货币都保留两位小数。
- SDK / 数据库返回 Decimal 时还需检查 is_finite、范围、币种与 scale；拒绝 NaN / Infinity。JSON 默认行为不能代替金额契约。
- 对外金额用十进制文本或协议约定的最小单位整数；不为了 JSON 方便转换成 float。
- 输入的多余小数是拒绝还是舍入由业务决定。本例拒绝，不能悄悄将 19.999 改成 20.00。
- 税、费、优惠的舍入发生在明细还是总单、采用 HALF_EVEN / HALF_UP / 向上取整，由业务规则决定并版本化。Python 用 localcontext 避免临时修改共享默认精度。
- Java BigDecimal 的 compareTo 比较数值，equals 还考虑 scale；作为 Map key 时约定数值规范化，不能混用等价关系。
- 科学计算、图形或允许误差的统计可使用浮点；仅对要求精确结算的领域施加金额规则。

依据：[Python Decimal](https://docs.python.org/3/library/decimal.html)、[Oracle BigDecimal](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/math/BigDecimal.html)。

## D2 分摊要证明总额守恒，整数方案仍要处理溢出

Bad：每人独立四舍五入，三份 0.33 不能覆盖 1.00。

```python
shares = [round(total / 3, 2)] * 3
```

Good：最小单位整数 + 最大余数法；相同余数按稳定业务顺序打破平局。示例限制总额、单个权重和明细数，实际范围由业务确认。

```python
MAX_TOTAL_FEN = 10**14 - 1  # 对应 D1 的金额范围
MAX_WEIGHT = 10**9
MAX_PARTS = 10_000

def allocate_fen(total_fen: int, weights: tuple[int, ...]) -> tuple[int, ...]:
    if (type(total_fen) is not int or not 0 <= total_fen <= MAX_TOTAL_FEN
            or type(weights) is not tuple or not 1 <= len(weights) <= MAX_PARTS):
        raise ValueError("invalid allocation")
    if any(type(w) is not int or not 0 <= w <= MAX_WEIGHT for w in weights):
        raise ValueError("invalid weight")
    denominator = sum(weights)
    if denominator == 0:
        raise ValueError("zero total weight")
    quotients = [divmod(total_fen * w, denominator) for w in weights]
    result = [q for q, _ in quotients]
    remaining = total_fen - sum(result)
    priority = sorted(range(len(weights)), key=lambda i: (-quotients[i][1], i))
    for index in priority[:remaining]:
        result[index] += 1
    return tuple(result)
```

结果满足 `sum(shares) == total_fen`。输入顺序必须由稳定的业务键确定，不能来自无序集合。退款可以按原分摊结果反向冲销，避免重算后尾差落到不同明细；会计规则不同则按领域契约处理。

Bad / Good：JVM 整数溢出不能靠改成 long 自动解决。

```java
// Bad：乘法先溢出，再赋值。
long subtotal = unitPriceFen * quantity;
// Good：unitPriceFen 为 long；溢出成为明确失败。
long subtotal = Math.multiplyExact(unitPriceFen, (long) quantity);
long total = Math.addExact(previousTotal, subtotal);
```

Python 大整数不溢出并不意味着没有资源成本；限制位数、权重数量和乘法输入。Java 超大范围可选 BigInteger / BigDecimal，但仍限制输入与资源。

## D3 时间：绝对时刻、日历日期、营业时区和经过时间分别建模

Bad：服务机器默认时区决定持久化含义，replace 把本地钟面直接改贴成 UTC。

```python
paid_at = datetime.now()
utc_paid_at = local_paid_at.replace(tzinfo=timezone.utc)
expires_at = paid_at + timedelta(days=30)  # 若业务实际指下个月，并不等价
```

```java
LocalDateTime paidAt = LocalDateTime.now();
```

Good：事件用明确时间点，时钟可注入；本地日期继续保留业务含义。

```python
from datetime import datetime, timezone

def as_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("timezone required")
    try:
        return instant.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError("UTC instant outside supported datetime range") from exc

paid_at = datetime.now(timezone.utc)
```

```java
Instant paidAt = clock.instant();           // 注入 Clock
LocalDate settlementDate = LocalDate.now(clock.withZone(businessZone));
Duration callBudget = Duration.ofMillis(250);
```

数据库列类型、会话时区、驱动编解码与 API 格式必须一致；MySQL DATETIME 本身不携带 IANA 时区。UTC 时刻适合事件排序，生日、营业日和“每月最后一天”需要 date / LocalDate 与业务时区，不能强行替换成 UTC 零点。

### DST：本地时间可能不存在，或者对应两个时刻

Bad：认为带上 ZoneInfo / atZone 就完成了歧义校验。

```python
instant = local.replace(tzinfo=ZoneInfo("America/New_York"))
```

```java
Instant instant = local.atZone(zone).toInstant();
```

Good：当接口只允许无歧义本地时间时，拒绝 gap / overlap；若业务允许重叠，要求调用方选择 offset / fold 并验证。

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

def local_to_utc_strict(local: datetime, zone: ZoneInfo) -> datetime:
    if local.tzinfo is not None:
        raise ValueError("expected local datetime without offset")
    candidates = set()
    for fold in (0, 1):
        attached = local.replace(tzinfo=zone, fold=fold)
        try:
            utc = attached.astimezone(timezone.utc)
            restored = utc.astimezone(zone).replace(tzinfo=None)
        except OverflowError as exc:
            raise ValueError("local time outside supported datetime range") from exc
        if restored == local:
            candidates.add(utc)
    if len(candidates) != 1:
        raise ValueError("nonexistent or ambiguous local time")
    return next(iter(candidates))
```

```java
static Instant resolveStrict(LocalDateTime local, ZoneId zone) {
    List<ZoneOffset> offsets = zone.getRules().getValidOffsets(local);
    if (offsets.size() != 1) {
        throw new IllegalArgumentException("nonexistent or ambiguous local time");
    }
    return ZonedDateTime.ofStrict(local, offsets.get(0), zone).toInstant();
}
```

查询营业日使用半开区间 `[该日当地起点, 下一日当地起点)`，分别转换成 UTC。不要以起点加 86400 秒代替下一日；日历跨度和真实经过时长可能不同。涉及当地零点不存在 / 重叠的地区也需定义策略。月底加月明确“夹到月底”或拒绝，不能用固定 30 天替代。边界年份还要检查 UTC 转换和“下一日/加月”是否越过运行时或数据库的可表示范围；拒绝越界，不截断成最大日期。

Bad / Good：耗时测量使用单调时钟，避免系统校时导致负数。

```python
# Bad
elapsed = time.time() - started_wall_time
# Good
started_ns = time.perf_counter_ns()
perform_work()
elapsed_ns = time.perf_counter_ns() - started_ns
```

Java 对应 `System.nanoTime()`。单调时钟的绝对数值不跨机器传输；跨服务传播协议支持的剩余预算或受控截止时间。时区数据库也有版本，测试与生产镜像应可追溯。

依据：[Python zoneinfo](https://docs.python.org/3/library/zoneinfo.html)、[ZonedDateTime gap / overlap 行为](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/time/ZonedDateTime.html)。

## D4 序列化：输出的是稳定协议，不是对象内存布局

### 大整数与金额

Bad：后端整数虽精确，浏览器 JSON.parse 后已经无法准确表示全部 64 位 ID。

```python
return {"order_id": order.id, "amount": float(order.amount)}
```

Good：在协议中使用字符串 ID 和十进制金额文本，不让前端先读成 Number 再转字符串。

```python
return {
    "order_id": str(order.id),
    "currency": "CNY",
    "amount": format(order.amount, "f"),
    "state": order.state.value,
}
```

Java 使用专用响应 DTO 的 String 字段或项目定制序列化器；不要全局把所有 long 改成字符串而破坏已有协议。JavaScript BigInt 也不能直接使用默认 JSON.stringify，需要协议转换。

### 未知字段、枚举与对象图

Bad：全局禁止所有未知字段破坏新旧消费者共存；直接序列化 ORM 双向关联造成循环 / 隐式查询。

```python
return order.__dict__
```

Good：命令入口与事件消费分别定义策略，输出投影为 DTO。

```python
class CreateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantity: Annotated[int, Field(strict=True, ge=1, le=999)]

class ExternalEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event_id: str
    schema_version: int

view = {"order_id": str(order.id), "customer_id": str(order.customer_id)}
```

这里 ignore 只表示容忍可选扩展字段；已知字段仍校验，schema_version 的不支持版本不能静默当成支持。需要转发 / 审计未知字段时按体积预算保留原始封套。枚举对外编码稳定，不用 ordinal / Python auto 值承担持久化协议。未知资金状态不随意映射成成功；若定义 UNKNOWN，只允许进入明确的隔离、告警或拒绝路径。

### JSON 解析也有边界

Bad：默认解析允许非标准 NaN，重复键后值覆盖前值，巨型正文提前占满内存。

```python
payload = json.loads(unbounded_body)
```

Good：网关 / 服务入口先限制正文、解压后尺寸与解析深度策略；对需要确定性键语义的协议拒绝重复键。

```python
import json
import math

def reject_constant(value):
    raise ValueError("non-finite JSON number")

def finite_json_float(raw):
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("JSON number outside finite float range")
    return value

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result

payload = json.loads(
    bounded_body,
    parse_constant=reject_constant,
    parse_float=finite_json_float,
    object_pairs_hook=unique_object,
)
encoded = json.dumps(response_dto, allow_nan=False)
```

parse_constant 只处理 NaN / Infinity 等特殊字面量；1e400 通过默认 float 解析也可能得到 inf，所以额外检查 parse_float。有限 float 仍有舍入和下溢；此通用数值策略不用于精确结算。示例不自带解压 / 字节 / 嵌套深度限额，必须由项目边界接通。金融输入采用 D1 的文本契约；不要让默认浮点解析在模型校验前破坏金额来源。签名协议按其规定的原始字节或规范化算法验证，不能自行 trim / 重排 JSON 后验签。

依据：[RFC 7493：I-JSON 数值互操作与重复键约束](https://www.rfc-editor.org/rfc/rfc7493)、[Python JSON](https://docs.python.org/3/library/json.html)。

## D5 Validation at the Gate：结构检查、业务规则和原子约束缺一不可

Bad：松散 dict、隐式类型转换，且身份由请求自行声明。

```python
async def create(payload: dict):
    return await repo.insert(payload["tenant_id"], int(payload["quantity"]))
```

Good：Pydantic 描述结构，身份从可信认证链路获得，业务规则由服务验证。

```python
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Line(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sku_id: Annotated[int, Field(strict=True, gt=0)]
    quantity: Annotated[int, Field(strict=True, ge=1, le=999)]

class CreateOrder(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    lines: Annotated[tuple[Line, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def unique_skus(self):
        keys = [line.sku_id for line in self.lines]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate SKU")
        return self

async def create(command: CreateOrder, principal):
    return await service.create(principal.tenant_id, command)
```

Good：Spring MVC 使用实际生效的 Bean Validation，嵌套对象和 null 分别校验。

```java
record LineRequest(@NotNull @Positive Long skuId,
                   @NotNull @Min(1) @Max(999) Integer quantity) {}
record CreateOrderRequest(@NotEmpty @Size(max = 100)
                          List<@NotNull @Valid LineRequest> lines) {}

@PostMapping("/orders")
OrderView create(@AuthenticationPrincipal UserPrincipal principal,
                 @Valid @RequestBody CreateOrderRequest request) {
    return service.create(principal.tenantId(), request);
}
```

鉴权链必须已要求身份存在。Pydantic strict 对 JSON / Python 模式有差异；Bean Validation 不会自动验证普通对象的所有内部调用。边界验证包括长度 / 数量 / 数值范围 / 必需字段，业务层负责归属、库存、状态与规则版本，数据库负责唯一性和并发条件。不能承诺“进核心层的数据 100% 永久合法”，因为业务状态会变化。

清洗只执行协议允许的规范化。展示名可按约定 trim；密码、Token、幂等键、签名原文、产品代码大小写不能随意改写。PATCH 区分未提供与 null，Python 用 model_fields_set；Java 需要显式 presence 表达，不能仅一个 nullable 字段混淆两种语义。

依据：[Pydantic strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/)、[Spring 请求体校验](https://docs.spring.io/spring-framework/reference/web/webmvc/mvc-controller/ann-methods/requestbody.html)。

# 维度四：异常处理与生产级可观测性

## O1 异常分类与十条执行规则

| 类别 | 语义与处理 |
|---|---|
| 可预期业务拒绝 | 余额不足、状态冲突、资源不存在；稳定错误码，按 API 契约映射，不用系统错误堆栈淹没日志 |
| 可恢复依赖失败 | 某些连接失败、超时、限流；还需业务可重放、总预算、尝试上限才能重试 |
| 数据或程序缺陷 | 不变量破坏、解码协议漂移、未预期异常；边界记录一次、返回受控错误，不能伪装成空数据 |
| 取消 / 停机 | 传播取消或中断，完成受控清理；不当作普通业务失败反复重试 |
| 未知提交结果 | 网络超时但远端可能已成功；使用幂等键、状态查询和对账，不能仅按超时推断失败 |

执行规则：

1. 不用裸 except / 空 catch / `except Exception: pass` 吞掉未知失败。
2. 在有恢复能力的层捕获具体错误；最外层可捕获未知 Exception 做协议转换，不能返回伪成功。
3. 按业务语义分类，不以异常名称包含 Timeout 就认定可重试。
4. 翻译异常保留 cause；原样传播用裸 raise，Java 包装保留原异常。
5. 同一失败的完整堆栈由一个边界负责记录；下层可补上下文，避免每层 log-and-rethrow。
6. 事务异常保留回滚语义；不能捕获后返回正常结果，再让事务提交。
7. 重试有幂等、时间和容量预算；预期业务拒绝、协议校验失败、取消不进入通用重试。
8. Python CancelledError / Java 中断按运行时契约传播；不把 BaseException / Throwable 一网打尽当作兜底。
9. 清理可能失败，不能无意丢失主失败；优先使用标准资源协议，保留因果或 suppressed，验证双失败路径。
10. 对外错误稳定、有限、不含内部 SQL / 堆栈 / 凭证；内部保留受控关联信息和必要诊断证据。

这些是行为约束，具体日志级别、状态码和可恢复条件由当前协议决定。

## O2 保留因果链，日志只在负责边界落地

Bad：既重复打印，又丢掉真实原因。

```python
try:
    await client.fetch()
except Exception as exc:
    logger.exception("fetch failed")
    raise RuntimeError(str(exc))
```

```java
catch (Exception e) {
    log.error("fetch failed", e);
    throw new RuntimeException(e.getMessage());
}
```

Good：适配器翻译它真正理解的错误，边界负责最终记录和映射。

```python
class PricingUnavailable(RuntimeError):
    pass

try:
    return await pricing_client.fetch()
except (httpx.TimeoutException, httpx.NetworkError) as cause:
    raise PricingUnavailable("pricing dependency unavailable") from cause
```

```java
// PricingUnavailable 构造器接收 (String message, Throwable cause)。
catch (HttpTimeoutException cause) {
    throw new PricingUnavailable("pricing dependency unavailable", cause);
}
```

外层按错误码生成响应，并在一个负责边界记录失败。日志事件不直接序列化异常对象；如异常消息可能带 URL / SQL / 敏感值，诊断采集需使用项目已有的脱敏与访问控制，堆栈不采集 locals。保留 cause 不等于允许把 cause 的全部文本发给用户。

Bad / Good：事务中吞错会改变提交行为。

```java
// Bad
@Transactional
public Result reserve() {
    try { return writer.reserve(); }
    catch (RuntimeException e) { return Result.failed(); }
}
// Good：失败穿过事务代理，外层再转换协议。
@Transactional(rollbackFor = ReservationRejected.class)
public Reservation reserve() throws ReservationRejected {
    return writer.reserve();
}
```

默认回滚规则、checked exception 与项目全局策略见 [Java 指南](java.md)。Python 使用 session_factory.begin 的退出语义，不在事务内将 SQL 失败转换成正常返回。

Bad / Good：停止信号不能变成“无数据”。

```python
# Bad
try:
    return await operation()
except BaseException:
    return None
# Good
try:
    return await operation()
except asyncio.CancelledError:
    raise
```

```java
// Bad：吞掉中断，工作继续运行
catch (InterruptedException ignored) { }
// Good：若方法不能继续声明 throws，恢复标志再抛出有因果的失败
catch (InterruptedException cause) {
    Thread.currentThread().interrupt();
    throw new WorkCancelled(cause);
}
```

Python `except Exception` 在现代 asyncio 中不捕获 CancelledError；不要因此以为所有取消都安全，代码仍可能吞 BaseException、阻塞事件循环或产生无主任务。Java 如果继续向上抛 InterruptedException，可以直接传播，不必先重复设置中断标志。

资源双失败优先让 Java try-with-resources 保留主异常和 suppressed；Python context manager 的退出异常也可能成为表面异常，检查链与关闭策略，不编写通用 `except: pass` 来假装解决。需要自定义异步清理时，要考虑再次取消和清理截止，不把 shield 后无人管理的任务留在后台。

## O3 全链路追踪：上下文隔离、跨边界传播、退出恢复

### Python 请求上下文

Bad：全局字典随最后一个请求改变。

```python
current_request["trace_id"] = inbound_id
await handler()
```

Good：ContextVar 保存小型关联值，使用 token 对称恢复；这是纯 ASGI 中间件，不依赖 BaseHTTPMiddleware 的任务边界行为。

```python
import re
import uuid
from contextvars import ContextVar

request_id = ContextVar("request_id", default="")
RID = re.compile(rb"[A-Za-z0-9._-]{1,64}")

class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        values = [v for k, v in scope.get("headers", []) if k.lower() == b"x-request-id"]
        rid = (values[0].decode("ascii")
               if len(values) == 1 and RID.fullmatch(values[0])
               else uuid.uuid4().hex)
        token = request_id.set(rid)
        try:
            await self.app(scope, receive, send)
        finally:
            request_id.reset(token)
```

边界另有请求头总大小上限。客户端关联 ID 仅用于追踪，不能作为身份、租户或鉴权依据。重复 / 非法 ID 不写原文日志。

Bad / Good：任意线程池不一定自动传播 ContextVar。

```python
# Bad：依赖未说明的线程上下文继承
await loop.run_in_executor(pool, blocking_work)
# Good：每次提交复制独立 Context，不能并发重入同一个 Context
from contextvars import copy_context
context = copy_context()
await loop.run_in_executor(pool, context.run, blocking_work)
```

asyncio.to_thread 会传播当前上下文；Task 在创建时复制上下文，但后续修改不反向同步。后台作业另定义身份生命周期，避免长期保留请求中的凭证和对象。

### Java 线程复用

Bad：MDC 只设置不恢复，复用线程污染下一任务。

```java
pool.execute(() -> {
    MDC.put("requestId", requestId);
    service.run();
});
```

Good：提交时捕获，执行时安装，finally 恢复工作线程原值；完整 withMdc 模板见 [Java 指南第六节](java.md)。

```java
Map<String, String> captured = MDC.getCopyOfContextMap(); // 提交线程
pool.execute(() -> {
    Map<String, String> previous = MDC.getCopyOfContextMap();
    try {
        if (captured == null) MDC.clear(); else MDC.setContextMap(captured);
        service.run();
    } finally {
        if (previous == null) MDC.clear(); else MDC.setContextMap(previous);
    }
});
```

WebFlux 使用 Reactor Context / 框架传播，不假设链路固定在同一线程。已有 OpenTelemetry / Micrometer 集成优先复用，避免两套上下文覆盖。

### RequestID 与 TraceID 不等价

请求 ID 是关联字段；TraceID / SpanID 有父子关系和传播协议。优先使用已部署的 OpenTelemetry 与 W3C Trace Context，通过 HTTP / MQ 的 carrier 做 extract / inject；不要每个服务随机生成“trace_id”后声称全链路已打通。

跨信任边界验证或按策略重建追踪上下文，限制 baggage；其中不放 Token、个人身份和无限业务载荷。业务 ID 可作为受控日志或 span 属性，不作为无界 metric label。

依据：[Python contextvars](https://docs.python.org/3/library/contextvars.html)、[SLF4J MDC](https://www.slf4j.org/apidocs/org/slf4j/MDC.html)、[OpenTelemetry Context Propagation](https://opentelemetry.io/docs/concepts/context-propagation/)。

## O4 日志：限制求值、载荷、敏感字段与积压

### 参数化不等于所有成本都惰性

Bad：级别关闭时仍计算、序列化和拼接大对象。

```python
logger.debug(f"orders={serialize_all_orders(orders)}")
logger.debug("orders=%s", serialize_all_orders(orders))
```

```java
log.debug("orders={}", serializeAllOrders(orders));
```

Good：先检查级别，只构造有上限的摘要。

```python
if logger.isEnabledFor(logging.DEBUG):
    summary = summarize_orders(orders, max_items=5)
    logger.debug("order_summary=%s", summary)
```

```java
if (log.isDebugEnabled()) {
    log.debug("order_summary={}", summarizeOrders(orders, 5));
}
```

摘要函数必须限制深度、字符串长度、数组数量，不能先全部序列化再截断。只为了计数就不遍历百万条对象；从已有批次元信息取得统计。

### 自动脱敏以结构化允许字段为主

Bad：记录整个请求、请求头、模型 repr 和异常原文，事后用一条正则试图兜底。

```python
logger.info("request=%r headers=%r", request_data, headers)
```

Good：在日志适配层统一生成允许字段事件；下例 order / principal 都来自已验证对象。

```python
event = {
    "event": "order_created",
    "order_id": str(order.id),
    "tenant_id": str(principal.tenant_id),
    "request_id": request_id.get(),
    "line_count": len(order.lines),
}
logger.info("order_created", extra={"event_data": event})
```

对高频事件设置采样策略。日志适配器固定允许字段、最大尺寸和编码方式；确需手机号等字段时使用经审核的字段级脱敏类型，Token / Cookie / Authorization / 密码默认不进入事件。异常消息、URL query、SQL 参数、第三方响应和 traceback locals 都是独立泄漏入口。采集器脱敏作为第二道防线，不替代源头约束。

不信任的换行和控制字符按结构化编码或限制处理，避免日志伪造。敏感标识如需稳定关联使用项目批准的受控假名化方案，不能认为对手机号做普通无盐哈希就匿名了。

### 级别、指标与采样

| 级别 | 默认判断依据 |
|---|---|
| DEBUG | 有界诊断细节，通常关闭或采样；不能默认输出凭证 |
| INFO | 需要运营关联的正常生命周期事件，控制热路径数量 |
| WARN | 降级、过载、恢复性异常或即将耗尽预算，按频率聚合 |
| ERROR | 未预期系统失败或需要处理的失败终态，由负责边界记录一次 |

预期业务拒绝通常不打印 ERROR 堆栈。重试每次都 ERROR 会制造告警风暴；记录尝试指标，最终失败由一个边界负责。特殊安全审计按独立保留要求执行，不能靠 DEBUG/INFO 定义可靠性。

Bad：每条请求创建一个日志上传任务，业务结束后积压继续增长。

```python
asyncio.create_task(remote_log.send(large_event))
```

Good：应用作用域有界缓冲，小事件非阻塞入队，满时计数；消费者受监督并限时关闭。

```python
try:
    log_queue.put_nowait(safe_small_event)
except asyncio.QueueFull:
    dropped_log_events += 1
```

队列和计数属于同一事件循环；导出 dropped 指标，不能静默丢失。条数上限之外限制事件字节。需要不可丢失的审计事件应有持久化业务协议，不能复制这个可丢诊断日志模板。同步写和阻塞队列也可能拖慢业务，应按事件可靠性选择。

指标标签使用有限的 route 模板、状态类、依赖名、结果枚举；实际 URL、用户 ID、订单 ID、异常消息不作为标签。耗时记录直方图和尾分位，结合请求量、错误、排队、拒绝、池占用；不能仅看平均值与 HTTP 200 掩盖降级。

依据：[Python Logging](https://docs.python.org/3/library/logging.html)、[SLF4J 参数化日志](https://www.slf4j.org/manual.html)、[OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)。

## V 定向验证

- 金额：文本 0 / 最大范围 / 多余精度 / 指数 / NaN，负数政策；分摊总额与稳定尾差；JVM 溢出。
- 时间：固定 Clock、跨月月底、UTC 转换、DST gap / overlap、23/25 小时营业日；tzdata 版本。
- JSON：大于 2^53-1 的 ID 往返、金额文本、重复键、未知字段 / 枚举、旧消费方、循环对象投影和输入大小。
- 入口：null / 缺省 / 0 / True / 字符串整数 / 重复 SKU；认证与跨租户；业务状态变化后的数据库约束。
- 异常：保留 cause、取消继续传播、checked exception 回滚、清理双失败；不重复记录同一堆栈。
- 追踪：并发请求、线程复用、嵌套任务、失败退出后恢复；HTTP / MQ 父子链路；拒绝凭证进入 baggage。
- 日志：关闭级别时不执行昂贵摘要，Token 和原始个人数据不出现，长字符串 / 控制字符有界，队列满可观测且业务语义明确。

仅报告实际执行结果；示例政策与阈值需按项目业务调整。来源核对日期：2026-10-30，持续维护文档不等于全部当日新发表。
