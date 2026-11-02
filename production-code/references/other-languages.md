# 其他语言与应用类型的适配

这是迁移决策指南，不声称与 Python / Java 参考具有相同深度。只加载实际项目的语言部分；先读 manifest、锁文件、编译目标、框架版本与仓库规范，再核对对应版本的官方资料。下列 Bad/Good 为局部模式，外部端口使用仓库已有实现。

## 目录

- TypeScript / Node / React：见 [typescript-node-react.md](typescript-node-react.md)，只读运行时校验、任务所有权、SSR 隔离或 Effect 对应小节。
- Go：见 [go.md](go.md)，只读 Transport / 响应体、errgroup 或 Rows 对应小节。
- Rust / Tokio；C# / .NET；C / C++；未列出的语言、移动端、嵌入式与数据任务。先检索章节，不因多语言项目而整篇读取。

## Rust / Tokio：所有权不等于远程操作可取消

Bad：在 async 执行器线程中运行长期阻塞计算。
```rust
async fn digest(bytes: Vec<u8>) -> Digest {
    expensive_sync_digest(bytes)
}
```
Good：明确阻塞工作所有权，并在入口做并发准入；示例位于 Result 返回函数中。
```rust
let permit = cpu_slots.clone().acquire_owned().await?;
let digest = tokio::task::spawn_blocking(move || {
    let _permit = permit;
    expensive_sync_digest(bytes)
}).await?;
```
cpu_slots 是应用作用域有限 Semaphore，等待队列也需准入/截止限制。permit 移入阻塞闭包，避免调用方超时后过早释放容量而计算仍在运行。已开始的 spawn_blocking 无法被 abort 杀掉；长期工作需协作停止，或放入受管理进程。Drop 通常是同步清理，不能假定丢弃 Future 就完成异步回滚；检查 select!/超时中的 cancellation safety。业务错误用 Result 与领域错误，避免请求路径 unwrap/expect；不要为跨 await 共享状态随意持有同步锁。

来源：[Tokio spawn_blocking](https://docs.rs/tokio/latest/tokio/task/fn.spawn_blocking.html)、[Tokio cancellation](https://tokio.rs/tokio/topics/shutdown)。

## C# / .NET：await、取消令牌与 DbContext 工作单元

Bad：同步等待阻塞线程，并发共享同一个 DbContext。
```csharp
var order = client.GetOrderAsync(id).Result;
await Task.WhenAll(ids.Select(id => db.Orders.FindAsync(id).AsTask()));
```
Good：按调用链传递 CancellationToken，单工作单元内顺序访问 Context。
```csharp
await using var db = await factory.CreateDbContextAsync(cancellationToken);
var order = await db.Orders
    .AsNoTracking()
    .SingleOrDefaultAsync(x => x.Id == id, cancellationToken);
return order is null ? NotFound() : Ok(order);
```
factory 为项目配置的 IDbContextFactory；所用 EF Core 版本须支持这些 API。并发独立查询各用 Context，并限制并发数。取消令牌需要被下游实际消费，Task.WhenAll 本身不提供所有失败场景下的兄弟任务取消；需要时显式组织 linked token、取消与等待。资源按 IDisposable/IAsyncDisposable 使用 using/await using；不要把所有方法改成 async void；HTTP 客户端沿用项目的长生命周期或 IHttpClientFactory 策略。

来源：[EF Core DbContext lifetime/thread safety](https://learn.microsoft.com/en-us/ef/core/dbcontext-configuration/)、[.NET task cancellation](https://learn.microsoft.com/en-us/dotnet/csharp/asynchronous-programming/cancel-an-async-task-or-a-list-of-tasks)。

## C / C++：生命周期、未定义行为与数据竞争优先于语法花样

Bad：返回借用已销毁局部对象的 view。
```cpp
std::string_view message() {
    std::string text = build_message();
    return text;
}
```
Good：返回拥有数据的值，让返回值优化/移动承担传递。
```cpp
std::string message() {
    return build_message();
}
```
区分 owning 与 borrowing，span/string_view 的使用期不能超过底层存储。C++ 用 RAII/unique_ptr/容器表达所有权；C 保留清晰 owner、长度/容量、统一错误清理路径。检查有符号溢出、越界、释放后使用、重复释放与异常安全。volatile 不是线程同步，原子变量也不能自动保护多字段不变量；自定义 memory_order 需给出 happens-before 论证，不确定时选已有同步原语并测量。

使用仓库编译器和标准版本，按更改选择 ASan/UBSan、TSan；不要声称一次 sanitizer 通过就证明没有 UB/竞态，也不要假设所有 sanitizer 可同时启用。

来源：[C++ Core Guidelines：RAII 与资源](https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines#Rr-raii)、[Clang ASan](https://clang.llvm.org/docs/AddressSanitizer.html)。

## 未列出的语言、移动端、嵌入式与数据任务

先识别执行模型：事件循环/线程/进程、ARC/GC/手动内存、UI 主线程限制、可暂停生命周期、实时期限、数据库/文件/硬件边界。再把核心不变量翻译为该生态的惯用机制：生命周期、错误传播、容量、权限、兼容与可验证性。查版本匹配的官方资料，不发明 API。

例如移动端关注生命周期取消、主线程阻塞、离线状态冲突；嵌入式关注中断上下文、动态分配预算、实时上界和外设所有权；数据脚本关注数据类型、可重跑、分块和失败恢复。这些项目不需要为“生产级”一词自动添加 Web 框架、消息队列或分布式锁。
