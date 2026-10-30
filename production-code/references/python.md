# Python / FastAPI / Asyncio 执行指南

仅在目标项目包含 Python 时读取。先检查 `pyproject.toml`、锁文件、Python 版本、ASGI 运行时及已采用的测试工具；示例使用 Python 3.11+、Pydantic 2、SQLAlchemy 2.x，不能在旧项目中悄悄升级运行时。以下代码是模式片段，`repo`、`sink` 等代表项目现有端口。

## 1. 选择执行模型，别让 async 外观掩盖阻塞

Bad：阻塞整个事件循环。
```python
@app.get("/report")
async def report():
    return legacy_sdk.blocking_report()
```
Good：若整条调用链只有同步 SDK，让 FastAPI 在线程池执行同步路由。
```python
@app.get("/report")
def report():
    return legacy_sdk.blocking_report()
```
若调用链原生异步，保留 `async def` 并调用异步驱动；混合链路可在有容量控制的适配层卸载阻塞 IO。FastAPI 对同步路由/依赖的线程池行为，不会自动应用于你直接调用的普通辅助函数。CPU 密集工作按测量结果选择进程、原生库或任务系统；不要假定 `to_thread` 总能突破解释器限制，也不要依赖 GIL 证明共享状态安全。

来源：[FastAPI async 技术细节](https://fastapi.tiangolo.com/async/#very-technical-details)。

## 2. 入口校验固定形状；业务授权仍需可信上下文

Bad：Python 注解不做运行时验证，`bool` 也可能被当作整数。
```python
async def create(quantity: int, tenant_id: int):
    return await repo.insert(tenant_id, quantity)
```
Good：校验数量，租户从认证主体获得。
```python
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field

class CreateItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    quantity: Annotated[int, Field(strict=True, ge=1, le=999)]

async def create(command: CreateItem, principal):
    return await repo.insert(principal.tenant_id, command.quantity)
```
Pydantic 的 strict 在 Python/JSON 输入模式下存在类型特例，按官方 conversion table 核对；`frozen` 是浅层约束，嵌套可变容器仍可变化。`T | None` 不等于“字段有默认值”；PATCH 用 `model_fields_set` 区分未提供与显式清空。写入入口可拒绝未知字段，消费外部事件则按兼容性契约选择忽略/保存，不能全局强制一种策略。

来源：[Pydantic strict mode](https://docs.pydantic.dev/latest/concepts/strict_mode/)、[Models](https://docs.pydantic.dev/latest/concepts/models/)。

## 3. Session 属于单个工作单元，不能被并发任务共享

Bad：共享的 AsyncSession 有事务、连接和身份映射状态。
```python
await asyncio.gather(*(load_one(session, key) for key in ids))
```
Good：每个独立任务获得自己的短生命周期 Session。
```python
async def load_one(factory, key):
    async with factory() as session:
        return await session.scalar(select(Item.id).where(Item.id == key))
```
在下一节的有界任务池中调用该函数；不要因 Session 独立就创建无限查询。若多个动作需要同一数据库事务，按顺序在同一 Session 执行。写入使用 `async with factory.begin()`，退出提交后才报告成功。返回脱离 Session 的 DTO/列投影，或显式加载所需关系，避免关闭后懒加载。ORM 优化、CAS、N+1、事务与游标细节见数据参考。

来源：[SQLAlchemy：Using AsyncSession with Concurrent Tasks](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html#using-asyncsession-with-concurrent-tasks)。

## 4. 同时限制排队量、工作任务数与执行预算

Bad：即便每个任务内有 Semaphore，先创建百万协程仍消耗大量内存。
```python
await asyncio.gather(*(fetch(key) for key in million_ids))
```
Good：异步输入 + 固定工作任务 + 有界队列。以下模板选择任一子任务取消即终止批次，并保留原始失败类型；支持 None 作为业务输入。
```python
import asyncio
import math


async def run_jobs(
    source, handle, *, workers=4, item_timeout_s=1.0, total_timeout_s=5.0
) -> None:
    """Consume a borrowed async source with bounded concurrent handlers.

    At most ``workers`` handlers and ``2 * workers`` queued items are retained,
    plus one item held by the producer. The item budget covers a handler call;
    the total budget includes reading and waiting for queue capacity.

    Failures and cancellation stop the batch and propagate after all tasks
    created here exit. Cancellation must be cooperative: blocking code or
    cancellation cleanup can exceed either budget. The caller owns the source
    resources and any idempotency required by handler side effects.
    """
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    for name, timeout in (
        ("item_timeout_s", item_timeout_s),
        ("total_timeout_s", total_timeout_s),
    ):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError(f"{name} must be a finite positive number")

    queue = asyncio.Queue(maxsize=2 * workers)
    stop = object()

    async def produce():
        async for item in source:
            await queue.put(item)
        for _ in range(workers):
            await queue.put(stop)

    async def work():
        while True:
            item = await queue.get()
            if item is stop:
                return
            async with asyncio.timeout(item_timeout_s):
                await handle(item)

    tasks = []
    try:
        async with asyncio.timeout(total_timeout_s):
            tasks.append(asyncio.create_task(produce()))
            for _ in range(workers):
                tasks.append(asyncio.create_task(work()))
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    # A child cancellation also aborts the batch, rather than
                    # silently losing a worker and leaving its queue blocked.
                    task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
```
workers 来自已验证的部署容量配置，不能由不受限的客户端输入决定。最多持有 workers 个处理任务、2*workers 个排队元素和生产者当前元素；source 的底层读取与 handle 输出也要有界。输入如果已是百万对象列表，该列表内存仍然存在。source 资源由调用者持有并按取消/退出契约关闭。

任务、超时和清理都是协作机制：阻塞事件循环、吞掉取消或无限清理会使退出超出预算。再次取消打断清理时，需要由应用运行时的受管任务组/关机协议继续监督，不能声称强制终止或绝对无泄漏。这里不捕获清理中的再次取消并悄悄伪装成功。

TaskGroup 适用于遵循其失败语义的结构化任务：普通子任务异常会取消并等待兄弟，并可能产生 ExceptionGroup；子任务单独以 CancelledError 结束不自动触发兄弟取消。固定 worker 若允许这种情况，需要明确监督，避免少了 worker 而生产者仍阻塞。模板通过等待每个任务结果显式处理，不把子任务取消当正常完成。

不要吞掉 CancelledError；asyncio.timeout 不会抢占无 await 的长计算；to_thread 取消等待不停止工作线程。已有 AnyIO/Trio 项目沿用其 task group/cancel scope，别混用不同取消模型。

来源：[Python TaskGroup、取消、timeout](https://docs.python.org/3.11/library/asyncio-task.html)。

## 5. 防御性契约与异常日志按专门参考执行

涉及金额、UTC/业务时区、DST、JSON 大整数、入口校验、异常分类或脱敏时，读取 [defensive-and-observability.md](defensive-and-observability.md) 的对应部分，不在语言适配层重复建立第二套契约。Python 特别检查 Decimal context 的作用域、非有限值、带时区对象、Pydantic coercion 和模型配置；类型注解与 NewType 不能替代运行时边界验证。

## 6. 流式是端到端属性；资源作用域覆盖实际消费

Bad：迭代器返回时响应已关闭；yield 之前全量读取也不算流式。
```python
async def open_chunks(client, url):
    async with client.stream("GET", url) as response:
        return response.aiter_bytes()
```
Good：消费期间持续持有响应，输出 await 提供背压。
```python
async def copy_body(client, url, sink):
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
            await sink.write(chunk)
```
`sink.write` 返回前必须消费该块或等待有界容量，不能偷偷追加无界列表。HTTP 客户端/Engine 在应用 lifespan 创建并关闭；响应/Session 在单次工作单元内关闭。FastAPI StreamingResponse 的真实消费发生在返回对象之后，不能提前关闭其依赖；还要处理上游错误映射与断连。异步生成器提前 break 使用 `contextlib.aclosing`；超大单行输入用有界 readline，检查前先整行分配仍可能 OOM。

来源：[HTTPX async / streaming](https://www.python-httpx.org/async/)、[aclosing](https://docs.python.org/3.11/library/contextlib.html#contextlib.aclosing)。

## 7. 装饰器保留契约，观测不能覆盖原始结果

Bad：只测协程创建，且丢失元数据。
```python
def timed(fn):
    def wrapper(*args, **kwargs):
        started = time.time()
        result = fn(*args, **kwargs)
        print(time.time() - started)
        return result
    return wrapper
```
Good：等待实际执行，finally 记录；emit 为应用注入的有界非 IO 事件入口。
```python
import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from time import perf_counter_ns
from typing import ParamSpec, TypeVar
P = ParamSpec("P")
R = TypeVar("R")

_EMIT_FAILURE_LIMIT = (1 << 63) - 1
_timing_emit_failures = 0

def timing_diagnostics() -> dict[str, int]:
    """Read this snapshot on the same event loop as the timed wrappers."""
    return {"emit_failures": _timing_emit_failures}

def timed(emit):
    def decorate(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        label = f"{fn.__module__}.{fn.__qualname__}"
        @wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            global _timing_emit_failures
            start = perf_counter_ns()
            outcome = "error"
            try:
                value = await fn(*args, **kwargs)
                outcome = "ok"
                return value
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            finally:
                try:
                    emit(label, outcome, perf_counter_ns() - start)
                except Exception:
                    # Observability failure must not replace the business
                    # return value, exception, or cancellation.
                    _timing_emit_failures = min(
                        _timing_emit_failures + 1, _EMIT_FAILURE_LIMIT
                    )
        return wrapper
    return decorate
```
emit 契约：同步、非阻塞，固定小字段、有界 queue.put_nowait、满时计数并丢弃观测事件，不抛可预期的满队列异常；消费者受监督并上报丢弃量。装饰器另外隔离普通观测异常，保留原返回值、业务异常和取消，并递增固定大小的进程内饱和计数；应用健康检查须读取 `timing_diagnostics()` 的失败计数并告警，不能把持续观测故障当成正常。包装器与诊断读取限定于同一事件循环；多线程、多事件循环须另设同步与聚合。该计数不保留异常对象，不发起可能继续失败的日志 IO，也不替代 emit 的满队列丢弃量。

这是受控观测端口的隔离边界，不是吞掉业务异常的通用模板。这里不捕获 KeyboardInterrupt/SystemExit 等 BaseException 控制信号，emit 也不得用 CancelledError 表示指标失败。wraps 维护反射元信息，ParamSpec 维护静态参数类型，两者不能互相替代；跨模块延迟注解和 FastAPI DI 仍需验证。

来源：[wraps](https://docs.python.org/3.11/library/functools.html#functools.wraps)、[ParamSpec](https://docs.python.org/3.11/library/typing.html#typing.ParamSpec)。

## 8. 本地缓存与请求上下文都要避免跨调用污染

Bad：缓存协程对象，或把请求身份写入全局字典。
```python
@lru_cache(maxsize=None)
async def load_user(user_id):
    return await repo.load(user_id)
current_user["tenant_id"] = tenant_id
```
Good：普通 lru_cache 用于有界、纯计算、不可变结果；请求上下文用 token 恢复。
```python
from contextvars import ContextVar
from functools import lru_cache

request_id = ContextVar("request_id", default="")

@lru_cache(maxsize=128)
def _parse_columns(columns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name.strip() for name in columns)

def parse_columns(columns: tuple[str, ...]) -> tuple[str, ...]:
    # Example protocol bounds: validate before LRU hashes/retains the key.
    if not isinstance(columns, tuple) or len(columns) > 64:
        raise ValueError("expected at most 64 column names in a tuple")
    if any(not isinstance(name, str) or len(name) > 128 for name in columns):
        raise ValueError("column names must be strings of at most 128 characters")
    return _parse_columns(columns)

async def dispatch(rid, handler):
    token = request_id.set(rid)
    try:
        return await handler()
    finally:
        request_id.reset(token)
```
缓存键/结果的单项大小也要在边界限制，条目数上限不等于字节上限。这里的 64 列、128 字符是示例协议限制，实际按业务契约设置；校验在缓存包装器外，不能先用巨大键做 LRU 哈希。请求体本身还需在解析/构造这些对象前受限，内部缓存函数不对不可信输入开放。LRU 无 TTL，线程安全也不等于同键只计算一次；异步 IO 缓存需要任务共享、取消、TTL、租户/权限/版本键策略，不能用这个片段替代。ContextVar 是上下文隔离，不是权限来源；Task 创建会复制上下文，新后台任务可能持有过时身份；线程传播按实际 API 核实，to_thread 和任意线程池不能混为一谈。

来源：[lru_cache 限制](https://docs.python.org/3.11/library/functools.html#functools.lru_cache)、[contextvars](https://docs.python.org/3.11/library/contextvars.html)。

## 验证选择

- 沿用项目的 Ruff/格式化器、mypy/Pyright、pytest 等现有命令；未配置时不擅自增加整套工具链。
- 对改变的行为验证边界输入、异常/取消传播、Session/响应关闭、跨请求租户隔离。纯函数优先直接断言不变量，IO 适配器使用小范围集成测试。
- SQLite 不能证明 MySQL 的锁/隔离/服务器游标行为；Mock 不能证明连接池等待、超时或 RSS 上界。需要这些结论时，在目标驱动和受控环境验证并记录数据规模、并发、延迟分位数。
