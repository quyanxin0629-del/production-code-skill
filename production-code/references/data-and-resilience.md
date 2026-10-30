# 数据、缓存与外部依赖：按故障边界实现

适用：改动触及 SQL/ORM、Redis、网络请求、消息消费或滚动发布。只读取涉及的章节；不要给纯前端或本地纯函数强行增加 Redis、MQ、重试和状态机。

## 使用前确认

- 从依赖锁文件、驱动配置和 schema 确认数据库版本、隔离级别、索引、客户端超时及现有事务边界；不要根据框架名称猜默认值。
- 先定义不变量：谁能修改哪条记录、失败是否可重放、重复输入应产生什么结果、缓存允许陈旧多久、消息是否要求按实体有序。
- 用真实查询计划、影响行数、目标驱动集成测试验证；没有目标数据库时，标注“代码审查/本地验证”，不要宣称已验证锁行为或生产吞吐。
- 示例是需要接入项目端口的模式；`repo`、`gateway`、`session_factory` 等适配器的实现及幂等语义必须在仓库中确认。

## 1. 短事务与外部副作用

**Bad：事务持锁等待远程支付；回滚无法撤销已经成功的扣款。**

```python
async with session_factory.begin() as session:
    order = await repo.lock_order(session, order_id)
    receipt = await gateway.charge(order)  # 网络不确定性进入持锁区
    await repo.mark_paid(session, order, receipt)
```

**Good：先持久化执行意图，以幂等业务键调用，再以条件更新落地结果。**

```python
async with session_factory.begin() as session:
    intent = await repo.create_or_get_payment_intent(session, order_id, request_key)
# intent 的创建、订单前置条件和 request_key 唯一约束在同一事务内。
receipt = await gateway.charge(intent, idempotency_key=intent.business_key)
async with session_factory.begin() as session:
    await repo.confirm_payment_if_pending(session, intent.id, receipt)
```

- `gateway` 必须真实支持相同幂等键绑定相同请求参数，并提供结果查询；本地 UUID 不会使外部扣款自动幂等。
- 记录超时后的未知结果，执行查询/对账；不能把“未收到响应”当作“未执行”。崩溃恢复依赖持久化意图及恢复任务。
- 有消息需求时，把 outbox 记录与业务更新一起提交；提交后直接发 MQ 仍存在进程崩溃导致的遗漏窗口。
- 对远程数据先取快照再进入事务时，进入事务后仍应重新验证影响正确性的版本和状态。

## 2. 锁范围与 CAS

**Bad：非原子的读改写，或无依据扩大锁定范围。**

```sql
SELECT remaining FROM inventory WHERE sku_id = :sku;
UPDATE inventory SET remaining = :previous - :quantity WHERE sku_id = :sku;
-- 另一种风险：在 RR 下对大范围未支付订单 FOR UPDATE 后逐条调用外部服务。
```

**Good：让存储原子执行前置条件，检查影响行数，提交后再报告成功。**

```sql
UPDATE inventory
SET remaining = remaining - :quantity, version = version + 1
WHERE tenant_id = :tenant AND sku_id = :sku
  AND remaining >= :quantity AND version = :expected_version;
-- tenant_id + sku_id 有唯一约束；quantity 在边界验证为正整数。
-- rowcount == 1 才成功；0 是未满足条件，不能无条件重试同一业务动作。
```

- `FOR UPDATE` 可用于范围业务，并非“必须唯一索引才合法”。现有行的完整唯一键等值查找通常只锁记录；非唯一/范围扫描在 RR 下可能有 next-key/gap locks。
- 完整唯一键查找不存在的行、复合唯一索引只匹配部分列、二级索引及额外唯一性检查，要分别检查实际锁行为。
- RC 通常减少搜索和扫描的 gap locking，但外键与重复键检查仍有例外；不要为性能随意修改全局隔离级别。
- 一致性快照读与锁定读不同；没有合适索引会扩大被扫描和锁定的范围，不能简单称作自动“升级成表锁”。
- 固定实体加锁顺序、缩短事务、限制等待；死锁重试应重跑完整事务，并排除无法安全重放的外部副作用。

来源：[MySQL 8.4：锁语义](https://dev.mysql.com/doc/refman/8.4/en/innodb-locks-set.html)、[隔离级别与间隙锁](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking.html)。按项目版本复核，不把示例版本当作项目升级要求。

## 3. N+1：查询次数必须随业务批次有界

**Bad：序列化或模板访问关系时，隐式触发逐条查询。**

```python
orders = (await session.scalars(select(Order).limit(100))).all()
return [serialize(order, order.items) for order in orders]
```

**Good：在查询边界声明所需关系，限制父结果并禁止未计划的懒加载。**

```python
from sqlalchemy.orm import raiseload, selectinload

stmt = (
    select(Order).where(Order.tenant_id == tenant_id).order_by(Order.id).limit(100)
    .options(selectinload(Order.items), raiseload("*"))
)
orders = (await session.scalars(stmt)).all()
```

- `selectinload` 可能按参数和驱动限制分批，不能承诺任何数据规模永远只有两条 SQL。
- MyBatis 可先查一页订单，再按这一页 ID 分批查明细并分组；不要用循环 `selectById` 修补关联。
- JOIN 多个集合可能产生笛卡尔膨胀；一条 SQL 不代表低成本。分页应先确定父集合，再加载明细。
- 使用查询计数或集成测试验证对象到 DTO 的全过程；不要只观察入口执行的那一条 SQL。

来源：[SQLAlchemy：Relationship Loading Techniques](https://docs.sqlalchemy.org/en/20/orm/queryguide/relationships.html)。

## 4. 游标分页：全序、过滤条件与索引必须一致

**Bad：深 OFFSET，或只用非唯一时间列作为游标。**

```sql
SELECT * FROM orders ORDER BY created_at DESC LIMIT 1000000, 20;
-- created_at < :last_created_at 会漏掉同时间但尚未返回的记录。
```

**Good：使用完整排序元组；第一页使用同过滤/排序但没有游标谓词。**

```sql
SELECT id, created_at, amount_cny_fen
FROM orders
WHERE tenant_id = :tenant
  AND (created_at < :last_time
       OR (created_at = :last_time AND id < :last_id))
ORDER BY created_at DESC, id DESC
LIMIT :page_size;
-- 候选索引：(tenant_id, created_at, id)，以目标版本的 EXPLAIN 验证。
```

- 游标包含排序元组、过滤条件版本及必要的完整性保护；页大小需限幅，解码后仍做权限检查。
- nullable 排序列必须定义 NULL 的顺序并在谓词中一致处理；优先选择不可空的稳定排序键。
- 排序字段在浏览期间修改会导致移动、遗漏或重复；严格导出需要快照/版本边界，单靠 seek 不保证快照一致。
- 需要跳任意页时评估延迟关联：先用覆盖索引找到该页 ID，再回表，但它仍需扫描 OFFSET，不能宣称常数成本。
- 验证 rows examined、排序、回表数量和结果边界；优化器是否采用复合索引，以实际计划为准。

来源：[MySQL：LIMIT 优化](https://dev.mysql.com/doc/refman/8.4/en/limit-optimization.html)。

## 5. 批量写入与可索引谓词

**Bad：每行提交，或将任意大输入一次拼成 SQL；日期函数包住索引列。**

```python
for row in rows:
    await session.execute(insert(Order), row)
    await session.commit()
```

```sql
SELECT id FROM orders WHERE DATE(created_at) = :date;
```

**Good：逐批提交可独立恢复的批次，时间条件按半开区间表达。**

```python
async for batch in source.iter_batches(max_rows=500, max_bytes=512_000):
    async with session_factory.begin() as session:
        await session.execute(insert(Order), batch)
    await checkpoint.store(batch.last_source_position)
```

```sql
SELECT id FROM orders
WHERE created_at >= :start_utc AND created_at < :next_start_utc;
```

- `iter_batches` 与 checkpoint 是项目端口；批次重放需要业务唯一键/幂等 upsert，检查点失败不能造成重复业务效果。
- 500 行只是起点；按编码字节、参数数量、`max_allowed_packet`、事务时长、复制延迟和锁竞争调整。批次提交会改变整体原子性，需先确认业务可分片。
- upsert 的唯一键与更新列必须明确；`REPLACE` 可能删除再插入，不是普通 update 的等价替代。
- 值用绑定参数且与列类型相符；动态表名和排序字段不能靠值绑定解决，需静态映射/白名单。
- 函数索引、生成列、Index Merge 等可能改变访问路径；不能把“OR/NOT IN/函数一定索引失效”作为铁律。`NOT IN` 还需检查 NULL 语义。

## 6. Redis 数据边界、TTL 与渐进遍历

**Bad：单键无界增长、同步全量返回，直接把不存在的 TTL 当作持久化策略。**

```python
await redis.hset("orders", mapping=all_orders)
all_items = await redis.hgetall("orders")
keys = await redis.keys("*")
```

**Good：按租户与业务分桶，限定单次载荷，增量遍历使用幂等处理。**

```python
import random

key = f"orders:v2:tenant:{tenant_id}:bucket:{bucket_id}"
async with redis.pipeline(transaction=True) as pipe:
    pipe.hset(key, mapping=bounded_batch)
    pipe.expire(key, base_ttl_s + random.randrange(jitter_s + 1))
    await pipe.execute()

async for field, value in redis.hscan_iter(key, count=200):
    await handle_idempotently(field, value)
```

- 示例为 redis-py asyncio；分桶函数需跨进程稳定，不能使用受进程随机化影响的 Python `hash(str)` 作为持久化路由。
- BigKey 治理按字节数、元素尺寸、命令延迟、热度、删除成本及复制影响制定预算；“一万个元素”不是统一安全边界。
- 压缩有 CPU、编码版本及解压大小限制；压缩后更小也不代表整键读写适合热路径。
- 缓存通常要 TTL；持久化幂等记录、业务状态、审计数据有独立保留策略，不能统一套缓存 TTL。
- HSCAN/SCAN 可能重复、返回空批，COUNT 是工作量提示，完整遍历以游标回到 0 为准；它不是一致性快照。
- Cluster 的扫描范围与路由由客户端决定，需遍历相关分片；分桶与 hash tag 选择要平衡热点与同槽原子操作。
- 常规请求路径不使用 KEYS、FLUSHALL 或无界 HGETALL/SMEMBERS；受控运维操作必须有单独范围与授权。

来源：[Redis SCAN](https://redis.io/docs/latest/commands/scan/)、[Redis Keyspace](https://redis.io/docs/latest/develop/using-commands/keyspace/)。

## 7. 缓存一致性与三类失效

**Bad：先删除缓存再更新 DB，或把缓存删除与 DB 提交当成原子操作。**

```python
await redis.delete(cache_key)
await repo.update_order(order_id, patch)  # 并发读可能重新写回旧数据
```

**Good：业务更新与失效意图同事务，提交后投递，失败由持久化任务恢复。**

```python
async with session_factory.begin() as session:
    version = await repo.update_if_version(session, order_id, expected, patch)
    await outbox.insert(session, event_id, "order.invalidate", order_id, version)
# relay 在提交后投递；消费者幂等删除缓存或按版本淘汰。
```

- Cache-Aside 的 DB 更新后删除缓存仍有旧读晚回填、删除失败和事件乱序窗口。根据允许陈旧时间选择 TTL、版本化缓存、可重放失效事件或 CDC。
- 延迟双删只是减小部分竞态窗口，不是强一致性证明；强一致读应读取权威存储或使用明确的版本验证协议。
- 穿透：缓存“确实不存在”的短 TTL 哨兵，区分依赖失败；Bloom Filter 只用于可接受误判的前置过滤，注意新增数据同步。
- 击穿：按键 single-flight/租约控制重建，等待和刷新并发都要有界；逻辑过期需定义最长陈旧期与刷新失败策略。
- 雪崩：TTL 抖动、预热、依赖 bulkhead 与降级预算共同限制回源；所有 key 同 TTL 会同步失效，随机 TTL 也不能解决 Redis 整体故障。
- 业务更新期间的鉴权、租户与版本不能因缓存命中而跳过。负缓存必须隔离租户并防止长时间遮蔽新建记录。

## 8. Pipeline 与 Lua 的不同保证

**Bad：多次 RTT，且读后写存在竞态；普通 pipeline 不会自动使其原子。**

```python
stock = int(await redis.get(stock_key) or 0)
if stock >= quantity:
    await redis.decrby(stock_key, quantity)
```

**Good：把有限的检查与修改放在一个脚本；返回约定码。**

```lua
local quantity = tonumber(ARGV[1])
local stock = tonumber(redis.call('GET', KEYS[1]))
if not quantity or quantity < 1 or quantity % 1 ~= 0 then return -2 end
if not stock or stock < quantity then return -1 end
return redis.call('DECRBY', KEYS[1], quantity)
```

- 此脚本只保证一次 Redis 执行不被其他命令穿插；请求超时后重放仍会再次扣减，需独立幂等键和结果记录。
- Lua 中预先验证参数、键类型及数值范围，限制在可安全精确计算范围；脚本运行错误不提供数据库式自动回滚。
- Lua 执行期间阻塞其他活动，禁止大循环/无界集合遍历；Cluster 脚本键须满足同槽约束。
- 批量独立 GET/SET 用有界 pipeline 减少 RTT；逐项检查响应，限制命令和响应字节；Cluster 可能分拆到多连接，不能声称永远一次 RTT。
- redis-py 的 pipeline 是否默认事务化要按客户端确认；需要非事务批处理时显式 `transaction=False`，不要把 pipeline、MULTI/EXEC 与脚本混为一谈。

来源：[Redis Pipelining](https://redis.io/docs/latest/develop/using-commands/pipelining/)、[Lua 执行语义](https://redis.io/docs/latest/develop/programmability/eval-intro/)。

## 9. 分布式锁：所有者解锁和资源侧 fencing 分别解决不同竞态

**Bad：锁过期后，旧持有者删除新持有者的锁。**

```python
await redis.set(lock_key, "locked", nx=True, px=lease_ms)
await perform_work()
await redis.delete(lock_key)
```

**Good：检查获取结果，使用随机所有者标识，并原子比较后删除。**

```python
owner = uuid.uuid4().hex
acquired = await redis.set(lock_key, owner, nx=True, px=lease_ms)
if not acquired:
    raise Busy()
try:
    await perform_bounded_work()
finally:
    await redis.eval(UNLOCK_SCRIPT, 1, lock_key, owner)
```

```lua
-- UNLOCK_SCRIPT：只释放自己仍拥有的租约。
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
```

- 这仍不是资金/库存正确性的完整锁。暂停、网络分区或租约过期后，旧执行者可能继续写资源。
- fencing 需要单调递增令牌，并由被保护资源原子拒绝旧令牌；随机 UUID 不是 fencing token，Redis 自增令牌还要评估故障切换回退。
- watchdog 应定期原子核对 owner 再续期；续期失败或所有权丢失后停止新副作用，正在执行的远程动作仍需幂等和对账。
- 续期有时间上限，解锁失败需要观测；finally 中的清理异常不能无意遮蔽业务异常。优先使用项目已验证的锁组件。

来源：[Redis：Distributed Locks，含 fencing 与时钟边界](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/)。

## 10. 外部调用：总预算覆盖排队、连接、执行和退避

**Bad：默认配置未核对；多层固定间隔重试放大流量。**

```python
while True:
    try:
        return await client.post("/charge", json=payload)
    except Exception:
        await asyncio.sleep(0.1)
```

**Good：仅对可安全重放的读操作采用有界重试；底层适配器负责分类异常。**

```python
async def read_with_budget(operation, *, deadline, gate, max_attempts=3):
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    async with asyncio.timeout_at(deadline):  # deadline 使用当前 loop.time() 时钟域
        for attempt in range(max_attempts):
            try:
                async with gate:  # 有界等待；服务入口另有队列/任务容量限制
                    return await operation()
            except TransientReadFailure as exc:
                if attempt + 1 == max_attempts:
                    raise
                ceiling = min(0.05 * (2 ** attempt), 0.5)
                delay = random.uniform(0, ceiling)
                if exc.retry_after_s is not None:
                    delay = max(delay, exc.retry_after_s)
                await asyncio.sleep(delay)
```

- `TransientReadFailure` 是适配器明确分类的异常，带经验证、非负的可选 `retry_after_s`；不要让所有 4xx、解析错误、授权失败、取消进入重试。
- 429/部分 5xx 是否可重试取决于契约与 Retry-After；POST 也可能凭服务端幂等协议安全重试，GET 也不该被假定没有副作用。
- 配置 connect/read/write/pool acquisition 等超时；读超时常按单次读取计算，不能替代整个请求截止时间。TLS、DNS、代理与连接池等待是否覆盖要查客户端文档。
- 重试只放在负责恢复的一层，并限制整体重试预算；取消继续传播。异步超时不证明远端停止处理，Java Future 超时也不证明工作线程已终止。
- HTTP/DB/Redis/MQ 客户端按组件生命周期复用；核心与报表/通知按故障域隔离容量。信号量不限制已创建的等待任务数量，入口还需 admission control。
- 队列按条数与字节设限，定义满队列的拒绝/丢弃/落盘行为；攒批按数量或最大等待时间触发，并定义关机排空期限。

来源：[AWS Builders' Library：Timeouts, Retries and Backoff with Jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)。

## 11. 消息：数据库提交、发送确认与消费确认不是同一件事

**Bad：数据库已提交但消息未发出；或消费前 ack 后进程崩溃。**

```python
await repo.commit_order(order)
await producer.send(event)  # 两者之间崩溃会漏消息
await broker.ack(message)
await apply_business_effect(message)  # ack 之后失败会丢处理
```

**Good：发送意图与业务同事务，消费去重与本地副作用同事务。**

```python
async with session_factory.begin() as session:
    await repo.save_order(session, order)
    await outbox.insert(session, event.id, event.type, event.payload)
# relay：有限批次 claim -> 发送并等待确认 -> 标记已发；崩溃可重复发送。

async def consume(message):
    try:
        async with session_factory.begin() as session:
            await inbox.insert(session, consumer_name, message.event_id)
            await apply_local_effect(session, message)
    except IntegrityError as exc:
        if not is_exact_inbox_duplicate(exc):
            raise
    await broker.ack(message)
```

- inbox 有 `UNIQUE(consumer_name, event_id)`；`is_exact_inbox_duplicate` 只接受该约束冲突，不能吞掉其他完整性错误。inbox 与业务效果必须同存储事务。
- outbox 标记和 broker 确认之间仍可能崩溃，所以消费者支持重复；事件 ID 应稳定，载荷有 schema_version、实体 ID、必要的实体序号。
- 外部邮件/支付等副作用不能加入本地 inbox 事务；使用下游幂等键或再次持久化意图并恢复，不能声称 inbox 解决全部 exactly-once。
- 根据可靠性要求设置发送确认、Kafka acks/副本最小同步数及 producer 幂等配置；核对实际版本默认值。Kafka 事务的保证不自动覆盖外部 MySQL。
- 消费提交在持久化效果之后；有界重试，区分暂态与毒丸。DLQ 持久化并获确认后才能推进原进度，告警并支持受控重放。
- 严格实体有序时，跳过毒丸进入 DLQ 会改变顺序语义；选择暂停对应实体/分区、隔离该实体或明确接受顺序降级。记录这个取舍。

来源：[AWS：Transactional Outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)、[Kafka：Delivery Semantics](https://kafka.apache.org/41/design/design/)。

## 12. 演进：允许新旧应用在迁移窗口共同运行

**Bad：一次发布同时改列名、移除旧字段、改变缓存 JSON。**

```sql
ALTER TABLE customers RENAME COLUMN name TO display_name;
-- 旧实例仍然查询 name；回滚后的应用也无法使用新 schema。
```

**Good：扩展 → 迁移 → 切换 → 清理，每一阶段有观测和回滚条件。**

```sql
ALTER TABLE customers ADD COLUMN display_name VARCHAR(200) NULL;
-- 第一阶段：所有读取仍以 name 为准，新实例同事务写两列；旧实例仍可只写 name。
-- 第二阶段：排空并退出所有只写 name 的旧实例/后台写入者，兼容新实例持续服务。
-- 第三阶段：以 name 为权威重新幂等回填 display_name；用版本条件防止覆盖并发新写。
-- 第四阶段：比对一致后切换读取；保留双写和回滚协议，窗口结束后再单独清理旧列。
```

- 以上是逻辑迁移步骤，不保证 ALTER 无锁；核对目标版本、ALGORITHM/LOCK 支持、元数据锁等待、磁盘及复制预算，必要时使用现有在线迁移工具。
- 字段缺省、null 与未知枚举各有协议语义；读端未知字段容错与写端严格校验可分别配置，避免“全部 extra=forbid”破坏消息兼容。
- 缓存使用版本化 namespace/envelope，新旧解码器并存并设置迁移期限；跨版本消息采用可回放转换，保持业务事件 ID 稳定。
- 回填结束不等于清理安全：确认后台任务、长寿命消费者、报表、CDC 和回滚版本均不再依赖旧结构。
- 只让新代码双写，不能阻止共存期旧实例把 name 改新而 display_name 留旧；不能据“新列非空”提前切换读取。若必须在旧写入者仍运行时读新列，需部署覆盖所有写路径的触发器 / 同步机制，明确原子性或可接受延迟并验证。
- 回填不能只填 NULL：共存期已填的新列也可能陈旧。停旧写入者后按权威旧列重算，并用版本 / 当前源值条件及冲突重试保护。回滚若恢复旧写入者，先恢复旧列权威读取或保持有效同步，避免再次产生陈旧新列。

## 交付时只报告与本次改动相关的证据

说明业务不变量、事务与重试边界、实际运行的测试/查询计划及尚未验证的目标环境；对未触及的章节保持安静。例子中的批量数、TTL、容量、重试次数均为待按项目预算确认的起点，禁止直接当作公司统一强制值。
