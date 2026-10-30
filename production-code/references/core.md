# 核心工程规则：维度一与维度二

按改动选择相关项。代码片段表达边界；业务模型、repo 和外部端口接到实际项目，不宣称片段构成完整应用。

## 目录

- C1 卫语句与检查顺序
- C2 表驱动、策略与状态机
- C3 抽象层次与纯计算
- C4 参数、空值、单位与布尔语义
- C5 集合、流与分配
- C6 生命周期与元编程

## C1 卫语句缩短检查与处理的距离

Bad：所有拒绝折叠成一个 None，主路径被嵌套包围。

```python
def checkout(user, cart):
    if user:
        if user.active:
            if cart:
                return create_order(cart)
    return None
```

Good：显式失败语义，主路径连续。

```python
def checkout(user, cart):
    if user is None:
        raise AuthenticationRequired()
    if not user.active:
        raise AccountDisabled()
    if not cart:
        raise EmptyCart()
    return create_order(cart)
```

保持认证、授权、存在性检查与副作用顺序；未授权者不能因重构获知敏感资源存在。扁平化不自动减少计算复杂度，也不解决检查后写入的竞态。

## C2 表、策略、状态机各自解决不同问题

Bad：未知渠道进入真实资金通道。

```python
handler = handlers.get(channel, default_payment_handler)
```

Good：有限组合显式查表，未知值在副作用前失败。

```python
try:
    handler = handlers[channel]
except KeyError as cause:
    raise UnsupportedChannel(channel) from cause
```

- 表驱动适合离散、不重叠的规则；范围、优先级、多重命中要定义冲突消解。
- 策略适合算法或依赖差异；仅参数不同先用数据。工厂启动时检查重复、缺失和配置，再发布只读注册表。
- 共享策略不保存请求状态；冻结映射不会自动冻结策略内部对象。`dict.get(key, Expensive())` 即使命中也创建默认对象。
- 状态机使用共享不可变迁移表，每个实例保存独立状态与版本。支付、履约、风控等正交轴不强行合成巨大枚举。

Bad：不同请求都能通过读取后的检查。

```sql
SELECT state FROM orders WHERE id = :id;
UPDATE orders SET state = 'paid' WHERE id = :id;
```

Good：原子写入同时保护业务前置条件与陈旧版本。

```sql
UPDATE orders SET state = 'paid', version = version + 1
WHERE tenant_id = :tenant AND id = :id
  AND state = 'pending' AND version = :expected_version;
```

检查影响行数，提交成功后再报告成功。零行按领域区分冲突、不存在或幂等重放；不继续执行资金动作。CAS 不证明外部扣款恰好一次，见 [数据指南](data-and-resilience.md)。

## C3 单一抽象层次与纯计算

Bad：定价算法逐条访问数据库和动态依赖。

```python
async def payable(order):
    total = 0
    for line in order.lines:
        total += await db.load_price(line.sku) * line.quantity
    return total - await redis.load_discount(order.user_id)
```

Good：适配器产生有一致性含义的快照，计算使用显式输入。

```python
def payable(lines, discount_cny_fen):
    subtotal = sum(x.price_cny_fen * x.quantity for x in lines)
    if not 0 <= discount_cny_fen <= subtotal:
        raise InvalidDiscount()
    return subtotal - discount_cny_fen

async def quote(command):
    snapshot = await pricing_source.load_snapshot(command)
    return payable(snapshot.lines, snapshot.discount_cny_fen)
```

时间、汇率、随机分组、规则版本也是输入。快照不因命名就一致；适配器定义读取版本和时点。抽函数应形成可命名业务动作、不变量或 IO 边界，不创建层层 process/do/handle 转发。

依据：[Google 2026：协作者与工作参数](https://testing.googleblog.com/2026/05/construct-with-collaborators-call-with.html)、[Google 2026：Prefactoring](https://testing.googleblog.com/2026/07/prefactoring-clear-way-for-your-new.html)。只对当前变更应用，不要求改造全部旧代码。

## C4 参数与返回值承载业务契约

Bad：平行数组、布尔开关和同类型位置参数。

```java
createOrder(userId, skuIds, quantities, true, false, amount);
```

Good：按概念聚合，复制容器建立请求快照。此为 JDK 17+ 结构示例，范围校验见防御性指南。

```java
record OrderLine(long skuId, int quantity) {}
record CreateOrder(long customerId, List<OrderLine> lines,
                   String currency, String idempotencyKey) {
    CreateOrder { lines = List.copyOf(lines); }
}
createOrder(command);
```

record / frozen 只浅冻结；元素也需满足可变性契约。稳定依赖通过服务构造器注入，不把 session、request、配置、身份和业务对象塞进万能 Context。

Bad：查询失败与零条结果合并。

```python
try:
    return await repo.list_orders(customer_id) or None
except Exception:
    return []
```

Good：成功零条返回集合；单个不存在显式处理，依赖异常传播。

```python
async def list_orders(customer_id) -> list[Order]:
    return await repo.list_orders(customer_id)

async def require_order(order_id) -> Order:
    order = await repo.find_order(order_id)  # Order | None
    if order is None:
        raise OrderNotFound(order_id)
    return order
```

Java Optional 不使用无条件 get；集合为空不代表未加载、无权限或服务失败。PATCH 缺省、null、具体值分开建模。

Bad / Good：正向谓词与 None 判断避免掩盖合法零值。

```python
# Bad
if not user.is_not_verified: ...
retry_count = supplied_retry_count or 3
# Good：本契约允许零次重试
if user.is_verified: ...
retry_count = 3 if supplied_retry_count is None else supplied_retry_count
```

金额命名含币种与单位；边界 timeout_ms，内部可用 Duration / timedelta，适配时转换；区分时间点与时长。状态编码使用枚举，算法中 0/1 不机械枚举化。正向谓词仍可以在卫语句中写 if not allowed。

## C5 集合、流与分配成本

Bad / Good：高频查询连构建成本一起计算。

```python
# Bad：O(N*M)，每轮 set(allowed_list) 也反复付构建成本
accepted = [x for x in items if x.id in allowed_list]
# Good：平均 O(M+N)，额外 O(M) 内存
allowed = set(allowed_list)
accepted = [x for x in items if x.id in allowed]
```

哈希平均 O(1) 不保证所有输入的最坏情况；计算哈希和相等也可能昂贵。小集合单次查找无需转换。保序去重可用 dict.fromkeys / LinkedHashSet，但精确全局去重状态仍随不同元素数增长。

Bad / Good：避免累计复制与无用中间容器。

```python
# Bad
for item in items:
    result = result + [transform(item)]
total = sum([cost(x) for x in items])
# Good
for item in items:
    result.append(transform(item))
total = sum(cost(x) for x in items)
```

append 均摊常数成本不表示单次扩容无延迟尖峰。简单纯映射适合推导式 / Stream；复杂副作用和重复昂贵计算改成命名步骤。Stream peek 不承载必须执行的业务副作用，parallel 不共享写普通容器，异步推导式不自动并发。

## C6 生命周期与元编程

Bad：资源关闭后返回依赖资源的迭代器。

```python
async def body(client, url):
    async with client.stream("GET", url) as response:
        return response.aiter_bytes()
```

Good：作用域覆盖真实消费，sink 返回前提供背压。

```python
async def copy(client, url, sink):
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            await sink.write(chunk)
```

文件、游标、Session、连接池和锁分别确定所有者。清理与提交不是同一保证；提前结束生成器用 closing / aclosing 或等价机制。输入单条大小、驱动抓取、输出缓冲和等待任务一起有界，才谈相对总量 O(1) 工作内存。

Bad / Good：异步包装要等待实际执行。

```python
# Bad：只创建 coroutine，计时和异常包装不会覆盖实际执行
def wrapper(*args, **kwargs):
    return async_function(*args, **kwargs)
# Good：元数据、类型与有界观测完整模式见 Python 指南
async def wrapper(*args, **kwargs):
    try:
        return await async_function(*args, **kwargs)
    finally:
        record_elapsed_without_blocking()
```

装饰器 / AOP 明确控制流、叠加顺序、鉴权位置和异常传播；导入 / 类加载阶段不连接外部依赖。普通 lru_cache 不直接缓存 async 函数、生成器或可变请求结果。本地缓存限制条目与单项字节，定义业务键和失效策略。

## 参考依据

- [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)：推导式、资源与装饰器选择边界。
- [CPython 容器复杂度](https://github.com/python/cpython/blob/main/Doc/builtins/time-complexity.rst)：按实现评估成本。
- [Python contextlib](https://docs.python.org/3/library/contextlib.html)：生成器确定性关闭。
- [Oracle Stream](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/stream/Stream.html)：非干扰、状态与 IO 源关闭。
- [SQLAlchemy 版本计数](https://docs.sqlalchemy.org/en/20/orm/versioning.html)：逐对象 flush 与批量 DML 的保护范围不同。
