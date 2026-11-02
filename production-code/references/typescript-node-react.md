# TypeScript / Node / React：边界、任务所有权与请求隔离

先确认 Node/Deno/Bun、浏览器或 SSR 的运行环境、manifest / 锁文件、tsconfig 与框架版本；只精读当前改动对应小节。下面是局部模式，端口和 schema 沿用仓库已有实现，不为普通 UI 引入服务端设施。

## 目录

- 1. 运行时输入与取消；2. Promise 的任务所有权；3. SSR 跨请求状态；4. React Effect 与响应竞态

## 1. 运行时输入与取消：类型断言没有校验网络数据

Bad：把 JSON 断言成类型，并误以为外层超时会结束请求。

```typescript
const response = await Promise.race([
  fetch(url),
  sleep(1500).then(() => { throw new Error("timeout"); }),
]);
const order = (await response.json()) as Order;
return order.amountCents;
```

Good：用项目既有 schema 在外部边界校验一次；信号同时覆盖 fetch 与读取响应体。以下以支持 AbortSignal.any / timeout 的目标运行时为前提，预算由调用链提供。

```typescript
const signal = AbortSignal.any([
  callerSignal,
  AbortSignal.timeout(upstreamBudgetMs),
]);
const response = await fetch(url, { signal });
if (!response.ok) throw new Error(`upstream status ${response.status}`);
const payload: unknown = await response.json();
const order = orderSchema.parse(payload);
return order.amountCents;
```

`as` 在编译后消失，不能阻止字段缺失或金额类型漂移；JS Number 也不能精确表示全范围 64 位整数，ID / 金额传输契约需事先确定。`readonly` 也不等于运行时深冻结。通过 schema 的内部业务无需逐层重新 parse。`Promise.race` 只选择结果；原网络操作没有收到取消信号仍会消耗连接与内存。即使有 signal，下游也必须实现取消，远端已提交的副作用不会因此回滚。响应大小仍需在网关/客户端流读取层限制，超时不等于内存上界。

来源：[TypeScript 类型收窄](https://www.typescriptlang.org/docs/handbook/2/narrowing.html)、[Node AbortSignal](https://nodejs.org/api/globals.html#class-abortsignal)。

## 2. Promise 的任务所有权：拒绝有人接，子任务有人收尾

Bad：其中一个分支失败或请求返回后，另一个任务的结果、异常和退出均无人负责。

```javascript
export async function loadPair(left, right) {
  void left();
  return right();
}
```

Good：固定两个只读、支持 signal 的下游，由同一父任务拥有。任何一个失败，发出兄弟取消，并等待两者结束后再交还控制权；同步抛错也转为受管理的 Promise。此处预算是已校验的应用配置，不在内部再堆类型检查。

<!-- example: node-owned-pair -->
```javascript
export async function loadPair(left, right, callerSignal, budgetMs) {
  const siblings = new AbortController();
  const signal = AbortSignal.any([
    callerSignal,
    AbortSignal.timeout(budgetMs),
    siblings.signal,
  ]);
  const pending = [
    Promise.resolve().then(() => left(signal)),
    Promise.resolve().then(() => right(signal)),
  ];
  try {
    return await Promise.all(pending);
  } catch (error) {
    siblings.abort(error);
    await Promise.allSettled(pending);
    throw error;
  }
}
```

`Promise.all` 自身不会取消兄弟。`allSettled` 用于收尾，不把部分失败伪装为成功；正常路径两份结果按入参顺序返回。若端口忽略 signal 或运行同步 CPU 死循环，等待仍可能不退出，不能宣称这段代码提供强制杀死任务的截止保证；此类工作需可终止 worker / 独立进程或调整端口契约。

固定两路并发不代表可以对百万项使用 `Promise.all(items.map(...))`；那会先创建全部 Promise。大输入选项目已有的有界 worker / 队列并处理背压，流式写入服从目标 API 的容量反馈。CPU 重计算不会因包装成 async 而离开事件循环，是否使用 worker 由测量及任务大小决定。必须完成的审计、付款、消息不能因“后台执行”而丢弃任务所有权：让请求 await，或先持久入队并由有界消费者与停机排空流程接管。单加 `void` 不处理拒绝；全局 `unhandledRejection` 是进程故障观测入口，不是业务补偿机制。Node 的拒绝行为还受版本与启动参数影响，不依赖它自动“兜底继续运行”。

来源：[Promise.all](https://tc39.es/ecma262/multipage/control-abstraction-objects.html#sec-promise.all)、[Node 未处理拒绝](https://nodejs.org/api/process.html#event-unhandledrejection)。

## 3. SSR 跨请求状态：模块活得比一次请求长

Bad：两个请求在 await 处交错后，A 渲染了 B 的身份。单线程事件循环不提供请求隔离。

```javascript
let currentPrincipal;
export async function renderAccount(credential, authenticate, load, render) {
  currentPrincipal = await authenticate(credential);
  const account = await load(currentPrincipal);
  return render({ principal: currentPrincipal, account });
}
```

Good：认证结果保留在请求局部变量，通过参数传递。只读配置与可并发复用的连接池可处于应用作用域，当前用户、租户和可变 SSR store 不可复用为进程单例。

<!-- example: node-request-local-account -->
```javascript
export async function renderAccount(credential, authenticate, load, render) {
  const principal = await authenticate(credential);
  const account = await load({
    tenantId: principal.tenantId,
    userId: principal.userId,
  });
  return render({ principal, account });
}
```

`load` 的存储查询仍须显式绑定可信 tenantId / userId；传对象本身不是授权实现。SSR/query cache 的 key 与生命周期按租户和身份授权范围设计，不把用户页跨请求缓存成公共数据。框架已有 request scope 时直接沿用。

需要无侵入读取 TraceID 时，可用请求作用域，仍不把它替代身份参数。

Bad：跨请求写入全局追踪值。

```javascript
let requestId;
export async function withTrace(id, handler) {
  requestId = id;
  return handler();
}
```

Good：应用级容器保存各异步调用链自己的只读上下文，入口用 run 建立作用域。

<!-- example: node-request-trace-scope -->
```javascript
import { AsyncLocalStorage } from "node:async_hooks";

const traces = new AsyncLocalStorage();
export function withTrace(id, handler) {
  return traces.run(Object.freeze({ requestId: id }), handler);
}
export function currentRequestId() {
  return traces.getStore()?.requestId;
}
```

上下文沿被创建的异步资源传播，不能假设自动穿过任意第三方回调/worker；跨端口按实际集成测试检查。不要在 store 塞整份 request、Token 或大对象，也不要让请求 scope 为无限后台任务续命。

来源：[Node AsyncLocalStorage](https://nodejs.org/api/async_context.html#class-asynclocalstorage)、[React 组件纯粹性与局部变更](https://react.dev/learn/keeping-components-pure)。

## 4. React Effect 与响应竞态：清理取消，还要阻止旧结果落地

Bad：渲染时订阅，重复渲染累积监听；请求 A 可能晚于 B 返回并覆盖 B 的状态。

```tsx
function Price({ feed, updatePrice, buy }: Props) {
  feed.subscribe(updatePrice);
  return <div onClick={buy}>Buy</div>;
}

useEffect(() => {
  loadOrder(orderId).then(setOrder);
}, [orderId]);
```

Good：项目订阅端口约定 subscribe 返回 `() => void`，因此 Effect 可直接返回它作清理；按钮使用原生交互语义。依赖中的回调稳定性由调用方设计，不能通过删依赖掩盖重执行。

```tsx
function Price({ feed, updatePrice, buy }: Props) {
  useEffect(() => feed.subscribe(updatePrice), [feed, updatePrice]);
  return <button type="button" onClick={buy}>Buy</button>;
}
```

数据加载优先沿用框架 loader/query/cache；确需手写 Effect 时，取消网络并禁止已经过期的闭包提交成功或失败状态。以下在组件内，`loadOrder(id, signal)` 是模块级稳定的 Promise 端口，`setView` 是 React 状态 setter；若端口来自 props 则也纳入依赖。状态与 UI 类型在项目中定义。

```tsx
useEffect(() => {
  const controller = new AbortController();
  let active = true;
  setView({ status: "loading" });
  async function load() {
    try {
      const order = await loadOrder(orderId, controller.signal);
      if (active) setView({ status: "ready", order });
    } catch (error) {
      if (active) setView({ status: "error", error });
    }
  }
  void load(); // load 已处理本端口的拒绝；setView 是 React 状态 setter
  return () => {
    active = false;
    controller.abort();
  };
}, [orderId]);
```

active 防止不支持取消的适配器或已进入后处理的旧请求写回；它不释放连接，AbortController 才向网络端口发出停止请求。StrictMode 的开发期重复 setup/cleanup 是生命周期检查，不应用全局“只跑一次”标志压掉。可直接计算的派生值不必复制成另一份 state。

按实际改动验证加载、空、失败/重试、重复提交、键盘焦点与可访问名称；不为纯 UI 改动读取 MySQL/Redis 全部章节。

来源：[React useEffect：清理与数据请求](https://react.dev/reference/react/useEffect)。
