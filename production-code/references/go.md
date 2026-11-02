# Go：连接复用、协作取消与资源所有权

先核对 go.mod / toolchain、驱动与 x/sync 版本；这里只迁移与当前改动有关的机制，不能据此声称语言全场景覆盖。

## 目录

- 1. HTTP Transport 与响应体；2. errgroup 的取消与等待；3. Rows 生命周期

## 1. HTTP Transport 与响应体：连接数、空闲数、并发数不是同一个量

Bad：每个请求创建 Transport，连接池无法跨请求复用；Client 无总预算。只解析一个 JSON 值后 Close，不一定读到 EOF。

```go
client := &http.Client{Transport: &http.Transport{}}
resp, err := client.Get(url)
if err != nil { return err }
defer resp.Body.Close()
return json.NewDecoder(resp.Body).Decode(&result)
```

Good：在应用启动处创建并复用 Client/Transport，优先克隆标准默认值而非从零丢掉拨号、TLS 与代理配置。下面假定 DefaultTransport 仍为标准 *http.Transport；若项目替换过全局 RoundTripper，沿用其显式构造端口。配置已在启动入口校验，值来自下游预算与压测，而非复制示例常数。

<!-- example: go-configured-http-client -->
```go
// imports: net/http, time
// Application-scoped; do not mutate fields after requests begin.
type HTTPConfig struct {
    MaxIdleConns       int
    MaxIdlePerHost     int
    MaxConnsPerHost    int
    HeaderTimeout     time.Duration
    RequestTimeout    time.Duration
}

func newHTTPClient(cfg HTTPConfig) (*http.Client, *http.Transport) {
    tr := http.DefaultTransport.(*http.Transport).Clone()
    tr.MaxIdleConns = cfg.MaxIdleConns
    tr.MaxIdleConnsPerHost = cfg.MaxIdlePerHost
    tr.MaxConnsPerHost = cfg.MaxConnsPerHost
    tr.ResponseHeaderTimeout = cfg.HeaderTimeout
    return &http.Client{
        Transport: tr,
        Timeout: cfg.RequestTimeout,
    }, tr
}
```

默认每 host 的空闲连接上限为 2；这不是只能并发 2 次。`MaxConnsPerHost=0` 不设总连接上限；提高 idle 上限不等于限制在途请求，更不限制 HTTP/2 多路复用的流并发。应用仍需准入/并发预算。`ResponseHeaderTimeout` 不覆盖响应体；Client.Timeout 或请求 Context 覆盖更长的请求生命周期。默认配置随 Go 版本核对，克隆后不要在热路径改共享 Transport，也不要修改全局 DefaultTransport 来影响不相关客户端。

响应体采用“有界读取并关闭”，不为保住一条连接而读完任意大/无限流。下面用于目的地及重定向已受既有边界策略约束的小型响应，maxBytes 是启动时确认 `0 < maxBytes < MaxInt64` 的配置；数据导出应流式处理，不套此小响应助手。

Bad：在循环所在函数统一 defer，直到整个批次结束才释放 body；或者无界 drain 不可信大响应。

```go
for _, url := range urls {
    resp, err := client.Get(url)
    if err != nil { return err }
    defer resp.Body.Close()
    if resp.StatusCode != http.StatusOK {
        io.Copy(io.Discard, resp.Body) // 没有字节上界
    }
}
```

Good：每次调用自己的作用域负责读取和关闭。超限时立即失败并关闭，即使牺牲本次连接复用；关闭/读取仍受请求预算约束。

<!-- example: go-read-bounded-response -->
```go
// imports: context, fmt, io, net/http
func readSmallResponse(ctx context.Context, client *http.Client,
    url string, maxBytes int64) ([]byte, error) {
    req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
    if err != nil { return nil, err }
    resp, err := client.Do(req)
    if err != nil { return nil, fmt.Errorf("request: %w", err) }
    defer resp.Body.Close()

    body, err := io.ReadAll(io.LimitReader(resp.Body, maxBytes+1))
    if err != nil { return nil, fmt.Errorf("read response: %w", err) }
    if int64(len(body)) > maxBytes {
        return nil, fmt.Errorf("response exceeds %d bytes", maxBytes)
    }
    if resp.StatusCode < 200 || resp.StatusCode >= 300 {
        return nil, fmt.Errorf("upstream status %d", resp.StatusCode)
    }
    return body, nil
}
```

在 HTTP/1.x 下，读取到真实 EOF 并 Close 才能为通常的 keep-alive 复用提供条件；远端关闭、协议错误等仍可能阻止复用。未读完就 Close 不能跨版本保证连接复用，某些新版本会尝试保守限额的自动异步 drain，不应依赖它处理任意响应。HTTP/2 还有独立 stream 生命周期，不能直接套用“丢一条 TCP 连接”的结论。此处多读一字节区别“完整且在界内”和“已截断”；仅用 LimitReader 读到人造 EOF 就宣称响应完整会漏报超限。错误响应也限制读取且不记录原始 body，避免大对象与敏感信息进入日志。重定向/SSRF 的目的地策略另见 [security-boundaries.md](security-boundaries.md)。

来源：[net/http Client、Transport、Body 契约](https://pkg.go.dev/net/http)、[io.LimitReader](https://pkg.go.dev/io#LimitReader)。

## 2. errgroup 的取消与等待：取消派生 Context 不等于杀掉 goroutine

Bad：Wait 成功后继续复用 groupCtx；它此时已被取消。另一种错误是某分支报错便直接返回，没等自己启动的工作退出。

```go
g, groupCtx := errgroup.WithContext(parent)
g.Go(func() error { return fetch(groupCtx) })
if err := g.Wait(); err != nil { return err }
return persist(groupCtx) // 即便全成功，Wait 也会取消 groupCtx
```

Good：派生 Context 只供这一组子任务。失败时所有已启动任务仍由 Wait 收尾；成功后续阶段使用仍有效的父预算。下例处理的是已限页的有界 IDs；parallelism 经入口确认大于零，fetch 响应取消并返回值，不偷偷启动无人等待的 goroutine。

<!-- example: go-owned-errgroup -->
```go
// imports: context; golang.org/x/sync/errgroup
// Order is the repository's domain type; fetch and persist are existing ports.
func syncOrders(parent context.Context, ids []int64, parallelism int,
    fetch func(context.Context, int64) (Order, error),
    persist func(context.Context, []Order) error) error {
    g, groupCtx := errgroup.WithContext(parent)
    g.SetLimit(parallelism)
    orders := make([]Order, len(ids))
    for i, id := range ids {
        if groupCtx.Err() != nil { break }
        i, id := i, id // Also valid for projects before Go 1.22 loop semantics.
        g.Go(func() error {
            if err := groupCtx.Err(); err != nil { return err }
            order, err := fetch(groupCtx, id)
            if err != nil { return err }
            orders[i] = order // Distinct index; no concurrent append/shared map.
            return nil
        })
    }
    if err := g.Wait(); err != nil { return err }
    if err := parent.Err(); err != nil { return err }
    return persist(parent, orders)
}
```

WithContext 在第一个非 nil 子任务错误或 Wait 返回时取消派生 Context；Wait 总是等待已启动的函数都返回。若下游忽略 Context，组仍可能卡住，不可因为发了 cancel 就先关闭其正在使用的 DB/client。调用方的总截止时间必须传入 HTTP/SQL 等实际端口，取消后可能已经发生的远端写需要幂等/恢复协议，不能当作回滚。

SetLimit 限制活动 goroutine，Go 方法在满额时阻塞；不是一个自动丢弃排队工作的限流器，也不能在任务运行中修改上限。示例在提交前与子任务入口检查取消，仍可能有一个任务在检查后等待准入，之后启动再立即退出。不要在占满同一个组的 worker 内再次调用阻塞的 g.Go，避免等待自身释放容量。无限输入需要固定 worker + 有界 channel，发送方负责 close，发送/接收阻塞需有 ctx.Done 退出路径；仍需由父任务等待 worker 结束。

来源：[errgroup WithContext / SetLimit / Wait](https://pkg.go.dev/golang.org/x/sync/errgroup)、[Go pipelines 与取消](https://go.dev/blog/pipelines)。

## 3. Rows 生命周期：循环结束不代表读取成功

Bad：查询没有请求预算，忽略扫描与迭代末尾错误。

```go
rows, _ := db.Query(query)
for rows.Next() {
    rows.Scan(&id)
}
```

Good：以下位于返回 error 的函数中；SQL 使用驱动支持的参数占位符，并按业务绑定租户/用户过滤。

```go
ctx, cancel := context.WithTimeout(parent, queryBudget)
defer cancel()
rows, err := db.QueryContext(ctx, query, tenantID, customerID)
if err != nil { return fmt.Errorf("query orders: %w", err) }
defer rows.Close()
for rows.Next() {
    var id int64
    if err := rows.Scan(&id); err != nil {
        return fmt.Errorf("scan order: %w", err)
    }
    if err := consume(ctx, id); err != nil { return err }
}
return rows.Err()
```

Rows.Next 返回 false 同时包含正常耗尽与读取故障，Rows.Err 用于区分；驱动取消能力需实测，写事务还需检查 Commit 等影响成功语义的返回错误。`error` 用 `%w` 保留因果，调用者用 errors.Is/As。成功路径直接返回 nil，避免 typed nil pointer 放入 error interface 后不等于 nil。共享 map 需同步；sync.Map 不自动让任意 check-then-act 原子。沿用仓库 gofmt / go vet / go test，涉及共享状态时运行相关 `-race` 测试；此文档本身不替代目标版本编译。

来源：[Go 数据库查询与 Rows.Err](https://go.dev/doc/database/querying)、[context](https://pkg.go.dev/context)。
