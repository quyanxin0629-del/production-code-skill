# Java / Spring / JUC / MyBatis 执行参考

按任务读取相关小节；不要一次照搬全部模式。下列代码是集成模式，`mapper`、`client`、DTO 与业务异常应接到仓库真实类型，完成编译后才能报告可运行。

## 适配版本与执行模型

- 先读 `pom.xml`、Gradle toolchain、依赖锁、运行镜像和 CI；Java 编译版本、运行版本、Spring Boot / Spring Framework 版本分别确认。
- 示例 `record` 适用于 Java 17 基线；虚拟线程正式 API 要求 Java 21+。不要为了用新语法擅自升级仓库。
- Spring Boot 3+ 常用 `jakarta.validation`；旧仓库可能使用 `javax.validation`，保持项目兼容性。
- 判断入口是 Spring MVC、WebFlux、消息消费者还是批处理；同一段阻塞代码在这些模型中的风险不同。
- 代理事务、命令式线程绑定事务、响应式事务不是相同传播模型。不要假设 `@Async` 新线程继承调用方数据库事务。
- 下面 12 组 Bad / Good 是决策参考；核心业务不变量、表结构和外部动作授权以当前任务为准。

## 1. 输入契约：缺失、零值、嵌套校验分别表达

**Bad**：原始类型把“字段缺失”折叠成 `0`，松散 Map 让业务层猜类型。

```java
record CreateOrder(long customerId, List<Map<String, Object>> lines) {}
@PostMapping("/orders")
OrderView create(@RequestBody CreateOrder request) { return service.create(request); }
```

**Good**：入口验证结构，命令快照明确复制；客户归属与库存仍由业务/数据库检查。

```java
record LineRequest(@NotNull @Positive Long skuId,
                   @NotNull @Min(1) @Max(999) Integer quantity) {}
record CreateOrder(@NotNull @Positive Long customerId,
                   @NotEmpty @Size(max = 100)
                   List<@NotNull @Valid LineRequest> lines) {}
record OrderCommand(long customerId, List<LineRequest> lines) {
    OrderCommand { lines = List.copyOf(lines); }
}
@PostMapping("/orders")
OrderView create(@Valid @RequestBody CreateOrder request) {
    return service.create(new OrderCommand(request.customerId(), request.lines()));
}
```

`record` 只限制字段重新赋值，不会冻结嵌套集合；`List.copyOf` 也不复制可变元素。本例元素是只含不可变值的 record。`@Positive` 不替代 `@NotNull`；请求字节上限与 JSON 数字/未知字段策略另在解析边界配置。
依据：[Spring 请求体验证](https://docs.spring.io/spring-framework/reference/web/webmvc/mvc-controller/ann-methods/requestbody.html)。

## 2. 金额、时间与可空返回：选择能表达契约的类型

**Bad**：二进制浮点先损失信息，缺少时区的本地时间不能唯一表示业务事件。

```java
BigDecimal amount = new BigDecimal(19.99);
LocalDateTime paidAt = LocalDateTime.now();
List<Order> orders = found ? rows : null;
```

**Good**：原始金额来自十进制文本，明确精度与舍入责任；时钟可注入。

```java
BigDecimal amount = new BigDecimal("19.99").setScale(2, RoundingMode.UNNECESSARY);
BigDecimal allocated = amount.divide(BigDecimal.valueOf(3), 2, RoundingMode.HALF_EVEN);
Instant paidAt = clock.instant();
Duration readTimeout = Duration.ofMillis(250);
List<Order> orders = List.copyOf(rows);          // 成功无记录时 rows 是空集合
Optional<Order> order = Optional.ofNullable(mapper.findById(id));
```

- 示例舍入规则不是通用财务规则；分摊必须另外处理尾差、币种和最小单位。`BigDecimal.valueOf(double)` 不能恢复上游已丢失的信息。
- `compareTo` 表达数值比较；`equals` 还比较 scale。选哪一个取决于业务等价关系，金额作为 Map key 时尤其要约定规范化。
- 使用整数最小单位时检查溢出，如 `Math.addExact`；不要把金额改成 `long` 后忽略范围。
- 单个结果允许不存在可用 `Optional`；必需存在则抛明确业务异常；数据库失败不能转换成空集合。
- 对外 JavaScript 消费者的大整数 ID 按协议传字符串；`Instant` 与本地日期/营业日不能互相替代。
依据：[BigDecimal](https://docs.oracle.com/en/java/javase/17/docs/api/java.base/java/math/BigDecimal.html)。

## 3. 集合与 Stream：避免乘法查找和共享写入

**Bad**：列表成员查询嵌在循环；并行流写共享 `ArrayList`。

```java
List<Long> accepted = new ArrayList<>();
orders.parallelStream().filter(o -> allowedIds.contains(o.id()))
      .forEach(o -> accepted.add(o.id()));
```

**Good**：只有需要高频成员查询才建立哈希索引，用归约构造结果。

```java
Set<Long> allowed = new HashSet<>(allowedIds);
List<Long> accepted = orders.stream()
        .filter(o -> allowed.contains(o.id()))
        .map(Order::id)
        .toList();
```

建立集合也要 O(M) 空间/时间；小集合单次查询可能不划算。`toList()` 是不可修改结果，且仍物化全部输出。并行流是否适合由 CPU 工作负载与测量决定，阻塞调用不要随意占 common pool。`peek(...).count()` 不能承载必须发生的业务副作用，执行阶段可能被优化跳过。
依据：[Stream API](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/stream/Stream.html)。

## 4. MVC 与 WebFlux：异步外形不改变阻塞操作

**Bad**：`Mono.just` 的参数在调用处求值，JDBC 先阻塞事件循环。

```java
Mono<Order> get(long id) {
    return Mono.just(jdbcRepository.find(id));
}
```

**Good**：已有 WebFlux 项目中的阻塞适配边界明确延迟执行与调度。

```java
Mono<Order> get(long id) {
    return Mono.fromCallable(() -> jdbcRepository.find(id))
            .subscribeOn(blockingIoScheduler);
}
```

`blockingIoScheduler` 由应用创建、限额与关闭，可采用项目管理的 bounded elastic scheduler；同时限制入口积压、数据库连接等待与查询超时。取消订阅不保证 JDBC 调用立即停止。已有 MVC + JDBC 项目可保持同步调用模型，不为“看起来现代”全量迁移响应式。响应式链路使用 Reactor Context / 框架上下文传播，不能只依赖线程 MDC。
依据：[Spring 并发模型](https://docs.spring.io/spring-framework/reference/web/webflux/new-framework.html)、[Reactor 阻塞适配](https://projectreactor.io/docs/core/release/reference/faq.html#faq.wrap-blocking)。

## 5. 事务与代理：通过真实代理进入短事务，验证回滚规则

**Bad**：同类自调用绕过代理；另一个调用路径一旦走代理，RPC 又被放进事务。

```java
public void place(Command command) { this.persist(command); }
@Transactional
public void persist(Command command) throws ReservationRejected {
    mapper.insert(command);
    quoteClient.fetch(command);
    throw new ReservationRejected();  // 假设为 checked exception
}
```

**Good**：编排组件调用独立事务 Bean；只持有数据库写入所需的事务范围。

```java
// OrderFacade：构造器注入 quoteClient 与 Spring 管理的 writer。
public Reservation place(Command command) throws ReservationRejected {
    Quote quote = quoteClient.fetch(command);
    return writer.reserve(command, quote);
}
// OrderWriter：另一 Spring Bean 的可代理公开方法。
@Transactional(rollbackFor = ReservationRejected.class)
public Reservation reserve(Command command, Quote quote) throws ReservationRejected {
    mapper.verifyQuoteVersion(quote.version());
    return reserveWithDatabaseConstraints(command, quote);
}
```

- 根据代理类型核实方法可见性、`final`、接口代理与 CGLIB 限制；不要靠同类 `this` 调用验证事务、缓存或权限切面。
- 默认通常是 `RuntimeException` / `Error` 回滚；checked exception、Spring 6.2+ 可配置全局规则及项目自定义规则都要检查。显式按异常类型配置，避免宽泛字符串匹配。
- 捕获异常后返回普通业务“失败结果”不会自动触发回滚；内层已 rollback-only 后继续外层提交可能出现 `UnexpectedRollbackException`。
- Spring 6.1+ 对事务方法**返回当时已经完成**的 `Future` / `CompletableFuture` 有特殊检查，例如 `CompletableFuture.failedFuture(ex)` 可按回滚规则触发回滚；不能把这个特例推广到稍后才失败或取消的任务。命令式事务不会等待一个尚未完成的 Future 来决定提交，提交后的取消也不能撤回数据库写入；`@Async` 和 `@Transactional` 的实际代理链与线程归属要在目标版本验证。
- 外部扣款不能靠本地事务原子化；使用业务幂等和 outbox/补偿方案。提交前不对外报告成功。
依据：[Spring 代理与自调用](https://docs.spring.io/spring-framework/reference/6.2/core/aop/proxying.html)、[回滚语义（6.2）](https://docs.spring.io/spring-framework/reference/6.2/data-access/transaction/declarative/rolling-back.html)。

## 6. MDC：提交时捕获，执行时安装，退出时恢复

**Bad**：复用线程残留身份与 TraceID，后续任务被错误关联。

```java
pool.execute(() -> {
    MDC.put("traceId", traceId);
    service.run();
});
```

**Good**：包装器在提交线程创建；恢复工作线程原上下文，支持嵌套执行。

```java
static Runnable withMdc(Runnable task) {
    Map<String, String> captured = MDC.getCopyOfContextMap();
    return () -> {
        Map<String, String> previous = MDC.getCopyOfContextMap();
        try {
            if (captured == null) MDC.clear(); else MDC.setContextMap(captured);
            task.run();
        } finally {
            if (previous == null) MDC.clear(); else MDC.setContextMap(previous);
        }
    };
}
pool.execute(withMdc(service::run));
```

优先用已有 tracing/上下文传播设施，避免重复包裹；只传播小型标量，不把整个请求或凭证放进 MDC。日志用参数化消息，但 `log.debug("{}", expensive())` 仍会先执行 `expensive()`；昂贵计算需条件保护或确认项目日志 API 的惰性参数能力。
依据：[SLF4J MDC](https://www.slf4j.org/apidocs/org/slf4j/MDC.html)。

## 7. 平台线程池：线程数、队列、拒绝与生命周期一起确定

**Bad**：工作线程有限，等待队列却无界；非核心报表与主链路争用同一个池。

```java
ExecutorService shared = Executors.newFixedThreadPool(16);
shared.submit(reportJob);
shared.submit(checkoutJob);
```

**Good**：应用作用域创建独立池，容量数字按工作负载配置。

```java
ThreadPoolExecutor reports = new ThreadPoolExecutor(
        4, 4, 0L, TimeUnit.MILLISECONDS,
        new ArrayBlockingQueue<>(100),
        namedThreadFactory("reports"),
        new ThreadPoolExecutor.AbortPolicy());
try {
    reports.execute(reportJob);
} catch (RejectedExecutionException saturated) {
    throw new ReportCapacityExceeded(saturated);
}
```

`namedThreadFactory` 使用仓库已有实现。实例由应用关闭并限时等待终止；不要每请求创建池。拒绝映射到明确的过载结果，不能默默丢弃关键任务。`CallerRunsPolicy` 会把工作转移到提交线程，可能阻塞 WebFlux 事件循环，不能当成通用背压。队列长度之外还要约束单任务持有的数据量、排队截止时间与完成率。
依据：[ThreadPoolExecutor](https://docs.oracle.com/en/java/javase/17/docs/api/java.base/java/util/concurrent/ThreadPoolExecutor.html)。

## 8. CompletableFuture：结果超时不是底层操作终止

**Bad**：默认 common pool 承担阻塞 IO；Future 超时后工作仍可能继续。

```java
return CompletableFuture.supplyAsync(() -> blockingClient.fetch())
        .orTimeout(200, TimeUnit.MILLISECONDS);
```

**Good**：对具体异步客户端保留原始操作句柄与有限请求预算。以下为 JDK HttpClient 状态探测；URI 来自可信配置，客户端在应用启动时复用并配置 connect timeout。

```java
CompletableFuture<HttpResponse<Void>> probe(
        HttpClient client, URI trustedUri, Duration budget) {
    long budgetNanos = budget.toNanos();  // 调用边界限制为可表示的有限时长。
    if (budgetNanos <= 0) throw new IllegalArgumentException("expired budget");
    long started = System.nanoTime();
    HttpRequest request = HttpRequest.newBuilder(trustedUri)
            .timeout(budget).GET().build();
    CompletableFuture<HttpResponse<Void>> operation = client.sendAsync(
            request, HttpResponse.BodyHandlers.discarding());
    long remaining = budgetNanos - (System.nanoTime() - started);
    CompletableFuture<HttpResponse<Void>> result = operation.copy()
            .orTimeout(Math.max(0, remaining), TimeUnit.NANOSECONDS);
    result.whenComplete((response, error) -> {
        if (error != null) operation.cancel(true);
    });
    return result;
}
```

- JDK 默认 HTTP 实现会尝试取消交换，不承诺即时释放，也不能撤回远端已执行的写入；此例使用只读 GET。
- 一次请求发起多个下游时分别设置预算；总请求预算采用单调时钟剩余时长，不能每个阶段重新得到完整超时。
- 组合方保留每个原始操作的取消句柄；任一必需分支失败、上游取消或后续提交被拒绝时，处理已启动的兄弟操作。
- 本例取消返回的 `result` 会请求取消 `operation`。普通 `CompletableFuture` 的下游阶段取消不会自动向上游传播，`allOf` 也要等所有输入完成才结束，并不自动快速失败或取消兄弟任务；组合方应显式连接这些退出路径。取消可能被包装在 `CompletionException` / `ExecutionException` 中，恢复逻辑不能把它误当作可降级的普通故障。
- 非 `async` 回调可能在完成 Future 的线程执行，超时回调还可能占用 JDK 的定时触发线程；这里仅发起非阻塞取消，不在 `whenComplete` 中执行阻塞清理或 RPC。
- `CompletableFuture.cancel(true)` 本身不提供任意计算的线程中断保证。对受管阻塞任务可持有 `Future` 并请求中断，但任务及客户端必须协作；底层 socket/query 仍需有限超时。
- 吞掉 `InterruptedException` 会破坏停机/取消；选择继续抛出，或包装为领域异常前先 `Thread.currentThread().interrupt()`。不要调用 `Thread.stop()`。
依据：[CompletableFuture 取消语义](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/concurrent/CompletableFuture.html)、[HttpClient 生命周期与取消](https://docs.oracle.com/en/java/javase/21/docs/api/java.net.http/java/net/http/HttpClient.html)。

## 9. 虚拟线程：放大等待并发，不放大下游容量

**Bad**：把线程创建成本降低误认为供应商能够处理无限请求。

```java
input.forEach(item -> virtualExecutor.submit(() -> vendor.call(item)));
```

**Good**：Java 21+ 虚拟线程的下游访问仍使用有界准入与截止时间。

```java
Result invoke(Item item) throws InterruptedException {
    if (!vendorPermits.tryAcquire(20, TimeUnit.MILLISECONDS)) {
        throw new RejectedExecutionException("vendor saturated");
    }
    try { return vendor.callWithTimeout(item); }
    finally { vendorPermits.release(); }
}
```

`vendorPermits` 为该依赖共享的 Semaphore，数量由容量预算确定。任务来源本身也要有背压，不能先创建百万等待任务。数据库池已有连接准入约束，不为同一边界机械叠加信号量；需要优先级/舱壁时再单独建模。不要为虚拟线程设置固定线程池来复用线程；不要把 JDK 21 的 pinning 细节不加版本区分地套到后续 JDK。
依据：[Oracle 虚拟线程指南](https://docs.oracle.com/en/java/javase/21/core/virtual-threads.html)。

## 10. MyBatis 参数：值用绑定，标识符用固定允许列表

**Bad**：SQL 字符串拼接允许外部输入成为语法。

```xml
<select id="find" resultType="OrderRow">
  SELECT id, amount_cny_fen FROM orders
  WHERE tenant_id = ${tenantId} ORDER BY ${sort}
</select>
```

**Good**：查询值绑定，排序只选择编译时固定片段。

```xml
<select id="find" resultType="OrderRow">
  SELECT id, amount_cny_fen FROM orders WHERE tenant_id = #{tenantId}
  ORDER BY
  <choose>
    <when test="sort == 'CREATED_DESC'">created_at DESC, id DESC</when>
    <otherwise>id DESC</otherwise>
  </choose>
  LIMIT #{limit}
</select>
```

边界验证 `sort` 为有限枚举、`limit` 在上限内；未知排序是拒绝还是默认必须明确。`#{column}` 不能把绑定值变成 SQL 列名。动态表名/列名如确需 `${}`，只能来自服务端封闭映射；SQL 参数化不能替代租户授权。XML 的 `<`、`&` 使用正确转义。
依据：[MyBatis 参数与字符串替换](https://mybatis.org/mybatis-3/sqlmap-xml.html#Parameters)。

## 11. 查询形态：批量装配、稳定游标与条件写入

**Bad**：Mapper 调用藏在循环里，深分页扫描巨大偏移。

```java
for (OrderRow order : orders) order.setItems(mapper.findItems(order.id()));
// SELECT ... ORDER BY id LIMIT 1000000, 20
```

**Good**：父记录本来已限页；一次取子记录再按 ID 分组；空 ID 列表提前返回。

```java
List<Long> ids = orders.stream().map(OrderRow::id).toList();
List<ItemRow> items = ids.isEmpty() ? List.of() : mapper.findItemsByOrderIds(ids);
Map<Long, List<ItemRow>> byOrder = items.stream().collect(
        Collectors.groupingBy(ItemRow::orderId));
// 下一页：WHERE tenant_id = #{tenantId} AND id > #{afterId}
// ORDER BY id ASC LIMIT #{pageSize}
// CAS：UPDATE orders SET state=#{next}, version=version+1
// WHERE tenant_id=#{tenantId} AND id=#{id} AND state=#{old} AND version=#{version}
```

多列排序游标需包含完整排序键和稳定 tie-breaker；页间快照一致性另行定义。`IN` 数量/批次按字节数、包大小、事务时长配置，500 只是候选值。CAS 必须检查影响行数并在提交成功后报告成功。`FOR UPDATE` 不要求永远命中唯一索引：唯一精确查找通常减小锁范围，范围/未命中查询的锁还取决于索引、隔离级别与执行计划。

## 12. Cursor：Session、游标、输出必须共享正确生命周期

**Bad**：方法返回时 Session 已关闭，或者把游标再次收集成完整 List。

```java
Cursor<OrderRow> openCursor() {
    try (SqlSession session = factory.openSession()) {
        return session.getMapper(OrderMapper.class).scan();
    }
}
```

**Good**：独立离线作业明确持有 Session 与 Cursor；输出同步施加背压。

```java
// Mapper：@Options(fetchSize = 500, resultSetType = ResultSetType.FORWARD_ONLY)
// XML 同样可设置 fetchSize="500" resultSetType="FORWARD_ONLY"。
try (SqlSession session = factory.openSession();
     Cursor<OrderRow> rows = session.getMapper(OrderMapper.class).scan()) {
    for (OrderRow row : rows) sink.writeRow(row);
}
```

`sink` 的生命周期由作业上层持有，不能把写入改成无界异步任务。Spring 管理的请求/事务内不另开未参与事务的原生 Session；选择现有集成方式并覆盖实际消费期。内存上界为批次 × 行大小 + 有界输出缓冲，游标全程占用连接。`fetchSize` 只是驱动提示；MySQL Connector/J 的游标方式需 `useCursorFetch=true` 且正 fetchSize，此路径会自动开启服务端预编译；不要混同旧式 `Integer.MIN_VALUE` 流式配置。目标驱动上测 RSS、提前退出关闭、慢输出和查询取消。
依据：[MyBatis Cursor](https://mybatis.org/mybatis-3/java-api.html)、[Connector/J useCursorFetch](https://dev.mysql.com/doc/connector-j/en/connector-j-connp-props-performance-extensions.html#cj-conn-prop_useCursorFetch)。

## 交付前的针对性验证

改动涉及共享内存、复合操作、分段锁、多键死锁或一致快照时，进一步读取 [JUC 锁与快照](juc-locks.md)。

1. 编译到仓库目标 Java 版本，执行对应模块测试；不能用本机较新 JDK 编译成功代替兼容性证据。
2. 涉及 AOP 的测试经过真实 Spring 代理；覆盖 checked exception 回滚、条件更新失败、提交后结果。
3. 并发测试验证过载拒绝、超时后真实占用、取消传播、线程上下文恢复与关闭；不用 sleep 猜时序，使用 latch/barrier。
4. 数据访问测试检查真实 SQL 与查询次数；涉及锁/游标/驱动参数时用目标数据库与驱动验证，内存数据库不能替代。
5. 只报告实际执行结果；未做 JMH、目标环境压测或故障注入时，不承诺固定加速倍数、百万 QPS 或“完全无泄漏”。

来源核对日期：2026-10-30。持续维护文档并非全部新发表实践；升级时按仓库版本重新核实相关行为。
