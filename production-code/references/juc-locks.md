# JUC：复合操作、分段锁与一致快照

用于 Java 17+ 共享内存状态；数据库与分布式一致性另按对应参考处理。先列出不变量及参与读写的所有路径，再选工具。不要从“读多”“用了并发容器”直接推导正确性。

## 1. 并发容器：单方法安全不等于多个调用组成原子事务

**Bad**：两个线程都可能通过检查，后一次替换使前一个计数器的增量丢失。

```java
if (!counters.containsKey(metric)) counters.put(metric, new LongAdder());
counters.get(metric).increment();
```

**Good**：仅用于有界指标名称集合、允许并发统计语义的计数器。

```java
ConcurrentHashMap<String, LongAdder> counters = new ConcurrentHashMap<>();
void record(String metric) {
    counters.computeIfAbsent(metric, ignored -> new LongAdder()).increment();
}
```

`computeIfAbsent` 的映射计算短小且无外部副作用；不要在 mapping 内执行 RPC、数据库查询、发送消息或递归修改同一映射，计算期间可能阻碍其他更新。返回 null 不建立条目，异常也不能被解释为已经初始化成功。已有异步 single-flight 组件更适合去重慢 IO。

不要将这个例子用于余额或库存：`LongAdder.sum()` 不是与并发更新原子一致的全局快照，计数器删除/替换也需要单独协调。单键状态的检查并修改可选 `compute`/CAS，但多个键共同维持的不变量不由 ConcurrentHashMap 自动保证；远端写入需另有幂等约束。
依据：[ConcurrentHashMap](https://docs.oracle.com/en/java/javase/17/docs/api/java.base/java/util/concurrent/ConcurrentHashMap.html)。

## 2. 固定分段锁：缩小冲突域，同时限制锁对象数量

**Bad**：所有业务 ID 争一个锁；或者为每个 ID 永久保存新锁导致锁表无界增长。

```java
synchronized (globalLock) { updateLocalState(orderId); }
// 另一陷阱：locks.computeIfAbsent(orderId, id -> new ReentrantLock()) 永不清理。
```

**Good**：应用中共享同一个 LocalStripes 实例，固定哈希映射；超时值只是示例预算。

```java
final class LocalStripes {
    private final ReentrantLock[] locks = IntStream.range(0, 256)
            .mapToObj(i -> new ReentrantLock()).toArray(ReentrantLock[]::new);
    int index(String businessKey) {
        return Math.floorMod(businessKey.hashCode(), locks.length);
    }
    void withKey(String key, Runnable update) throws InterruptedException, TimeoutException {
        ReentrantLock lock = locks[index(key)];
        if (!lock.tryLock(20, TimeUnit.MILLISECONDS)) {
            throw new TimeoutException("local contention");
        }
        try { update.run(); }
        finally { lock.unlock(); }
    }
}
```

业务 key 使用稳定、已规范化的标识，必要时包含业务域和租户。相同 key 必须命中同一实例的同一 stripe；不同 key 碰撞只增加竞争，不破坏互斥。锁数量和公平性依据争用测量选择，不把 256 当成固定最佳值。

分段锁也要求底层容器能承受不同 stripe 并行访问：不能用不同 key 的锁并发向同一个普通 `HashMap` 插入/删除。可选 `ConcurrentHashMap`，或每个 stripe 自有、仅在对应锁内访问的普通 Map。容器线程安全仍不等于业务复合操作原子化；同键的所有读写、删除、替换必须遵守同一个锁协议。

`update` 只做该锁保护的短小本地状态更新，不调用未知代码、不取得额外 stripe、不把 HTTP/文件/数据库 IO 包进去。可移出锁的计算先得到输入快照，持锁时验证版本再更新，避免使用已经过期的计算结果。

在多个 JVM、多个服务实例之间，本地锁完全不能建立互斥。即使 JVM 内加锁，数据库写入仍可能需要 CAS/唯一约束。未取得锁时不能 unlock；`InterruptedException` 向上传播，或恢复中断标记后转为明确失败。
依据：[ReentrantLock 与 timed tryLock](https://docs.oracle.com/en/java/javase/17/docs/api/java.base/java/util/concurrent/locks/ReentrantLock.html)、[HashMap 的外部同步要求](https://docs.oracle.com/en/java/javase/17/docs/api/java.base/java/util/HashMap.html)。

## 3. 多键操作：按去重后的 stripe 排序，并共享总等待预算

**Bad**：按调用参数顺序获取锁，A→B 与 B→A 可形成等待环；每把锁重置超时还会超出总预算。

```java
lockFor(from).lock();
lockFor(to).lock();
try { transferLocal(from, to); }
finally { lockFor(to).unlock(); lockFor(from).unlock(); }
```

**Good**：以下方法添加到上一节 LocalStripes；每条多键更新路径遵守同一排序协议。

```java
void withKeys(Collection<String> keys, Duration budget, Runnable update)
        throws InterruptedException, TimeoutException {
    long budgetNanos = budget.toNanos();
    if (budgetNanos <= 0) throw new IllegalArgumentException("expired budget");
    long started = System.nanoTime();
    int[] ordered = keys.stream().mapToInt(this::index).distinct().sorted().toArray();
    if (ordered.length == 0) throw new IllegalArgumentException("missing keys");
    int acquired = 0;
    try {
        for (int index : ordered) {
            long remaining = budgetNanos - (System.nanoTime() - started);
            if (remaining <= 0 || !locks[index].tryLock(remaining, TimeUnit.NANOSECONDS)) {
                throw new TimeoutException("local contention");
            }
            acquired++;
        }
        update.run();
    } finally {
        for (int i = acquired - 1; i >= 0; i--) locks[ordered[i]].unlock();
    }
}
```

先映射再去重：两个业务 key 可能落在同一个 stripe，不应将业务 ID 排序误认为锁对象已经排序。捕获异常、获取失败、线程中断均释放已经取得的锁。预算为经校验、不会溢出的有限时长；此处限制等待，不强制中断 `update`。上游若已有截止时间，传剩余预算，不能重新发放完整时长。

死锁的四个必要条件是互斥、持有并等待、不可强行剥夺、循环等待。固定全局锁序破坏循环等待；“失败即释放已获锁”限制持有并等待；超时提供退出路径，但不能替代所有路径遵守锁序。不能靠强制解锁其他线程或停止线程来破坏条件。

若 `update` 里再任意取得锁，整个证明失效。多数据库锁也应检查实际访问顺序与执行计划，JVM 的 stripe 顺序不会自动约束数据库锁顺序。

### 锁不提供异常回滚：先算出完整新状态，再修改

**Bad**：即便同时持有两把锁，第二步溢出仍会留下已经扣减的余额。

```java
balances.put(from, balances.get(from) - amount);
balances.put(to, Math.addExact(balances.get(to), amount));
```

**Good**：以下为进程内、预先初始化且不增删账户的余额模型；所有写入和需要一致结果的读取都由同一个 `stripes` 保护。余额用非负 `long` 最小单位，转入转出必须不同；这个例子维护非负和转账总额守恒，不承担数据库持久性。

```java
// 共享字段：private final ConcurrentHashMap<String, Long> balances;
// 共享字段：private final LocalStripes stripes;
void transfer(String from, String to, long amount, Duration budget)
        throws InterruptedException, TimeoutException {
    if (amount <= 0 || from.equals(to)) throw new IllegalArgumentException("invalid transfer");
    stripes.withKeys(List.of(from, to), budget, () -> {
        Long debit = balances.get(from);
        Long credit = balances.get(to);
        if (debit == null || credit == null) throw new IllegalArgumentException("unknown account");
        if (debit < amount) throw new IllegalStateException("insufficient balance");
        long nextDebit = Math.subtractExact(debit, amount);
        long nextCredit = Math.addExact(credit, amount);
        balances.put(from, nextDebit);
        balances.put(to, nextCredit);
    });
}
```

普通业务失败和算术溢出在首次写入前发现，`finally` 只负责解锁，不会恢复已经修改的数据。读取两账户合计也必须一次取得这两个 key 的锁；直接分别 `get` 或遍历 ConcurrentHashMap 可能看见转账中间状态。需要全表一致快照时，使用覆盖全体相关 stripe 的统一协议或下一节的不可变快照；不能把弱一致遍历当成业务快照。进程崩溃或 VM 级故障仍不具有事务回滚保证。

## 4. 读多写少：发布一致的不可变快照，或选择经验证的锁协议

**Bad**：原子引用里的对象仍然可变，读取多个字段时可能观察到不一致版本。

```java
AtomicReference<Map<String, Integer>> rates = new AtomicReference<>(new HashMap<>());
rates.get().put("vip", 500);  // 引用的原子访问不能保护内部 HashMap。
```

**Good**：只发布由不可变值构成的完整快照；更新函数允许重试，因此保持纯计算。

```java
record Rules(Map<String, Integer> rates, long version) {
    Rules { rates = Map.copyOf(rates); }
}
AtomicReference<Rules> current = new AtomicReference<>(new Rules(Map.of(), 0));
void setRate(String tier, int rateBps) {
    if (rateBps < 0 || rateBps > 10_000) throw new IllegalArgumentException("invalid rate");
    current.updateAndGet(previous -> {
        Map<String, Integer> next = new HashMap<>(previous.rates());
        next.put(tier, rateBps);
        return new Rules(next, Math.incrementExact(previous.version()));
    });
}
Rules snapshot = current.get();  // 一次操作内从这个快照读取所有相关字段。
```

AtomicReference 的 get/set 使用对应 volatile 内存语义，CAS/原子更新负责引用级的条件替换；锁的成功获取/释放建立相应可见性关系。它们不会把“读取、判断、修改多个共享对象”自动合成一个事务。`volatile count++` 也仍是读改写复合操作。

此例快照字段仅为不可变 Map 与不可变键值；嵌套可变实体必须复制或改成值对象。复制每次有 O(N) 分配成本，读者也可能延长旧快照存活；只适合规则表等尺寸受控的状态。更新函数内不能发送通知或修改外部对象，因为竞争时可能被调用多次；外部副作用在成功发布后按独立一致性契约处理。

读写锁仅在必须保护可变结构、读取临界区足够大且测量收益成立时考虑；短读取、写入频繁或写者延迟敏感时未必优于简单锁。不要以“读占 99%”直接决定使用读写锁，也不要在持有读锁时假设可以直接升级成写锁。

若使用 StampedLock 乐观读，先读取到局部变量，再验证 stamp；验证失败则持读锁重新读取。验证前不调用依赖一致结构的未知方法、不产生外部副作用。StampedLock 不可重入，不能照搬 ReentrantLock 的递归调用假设；只有数据表示和读写协议可证明正确时才选它。
依据：[AtomicReference](https://docs.oracle.com/en/java/javase/17/docs/api/java.base/java/util/concurrent/atomic/AtomicReference.html)、[StampedLock](https://docs.oracle.com/en/java/javase/17/docs/api/java.base/java/util/concurrent/locks/StampedLock.html)。

## 验证重点

用 latch/barrier 驱动同键竞争、反向多键竞争、stripe 碰撞、锁获取一半时中断/超时、更新异常后的解锁；验证最终不变量与后续请求仍可取得锁。转账另外覆盖余额不足、目标溢出后原状态不变，以及持相同多键锁读取时总额守恒。快照测试验证并发写入不丢更新、读者看见的是同一版本的完整状态。压力测试不能证明算法必然正确，计数正确也不代表跨进程一致性已经建立。

来源核对：2026-10-30；这些是针对指定 Java API 的机制约束，不承诺无锁算法、固定吞吐提升或零争用。
