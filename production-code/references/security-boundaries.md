# 外部输入到执行边界

仅在改动触及不可信输入及其执行边界时读取，不对纯格式、纯计算或无关 UI 改动发起全面安全重构。保留框架和项目已有安全设施；下面是需接到实际端口的模式，不是通用安全库。

## 目录

- 1. 固定集成 / SSRF；2. 文件 ID 与存储键；3. 子进程
- 4. 文本 sink / XSS；5. 反序列化；6. 秘密派生值比较；7. 租户、权限与资金状态；验证选择

## 1. 固定集成按服务标识选择，不把任意用户 URL 交给内部网络

Bad：简单检查字符串前缀不约束真实目的地、重定向或网络路由。

```python
if user_url.startswith("https://"):
    return await client.get(user_url, follow_redirects=True)
```

Good：业务只访问已配置供应商时，用户只能选择有限标识；地址在可信配置中，响应作用域覆盖使用。

<!-- example: provider-probe-python -->
```python
async def probe_provider(client, provider_id, provider_urls):
    # provider_urls 由可信启动配置提供，不合并请求中的 URL / host / headers。
    try:
        url = provider_urls[provider_id]
    except KeyError as cause:
        raise ValueError("unsupported provider") from cause
    async with client.stream("GET", url, follow_redirects=False) as response:
        if 300 <= response.status_code < 400:
            raise ValueError("unexpected redirect")
        response.raise_for_status()
        return response.status_code
```

客户端已有有限超时和连接容量；本例只检查状态，不加载任意响应正文。开放式下载 / webhook 配置若必须接受 URL，需要项目已验证的 fetcher：解析后的 scheme / host / port / userinfo、IPv4 / IPv6 和解析结果、代理、重定向各跳、连接地址与 DNS 校验一致性及出口网络策略一起约束。只校验 hostname 后再交给会重新解析的客户端，不能声称解决 DNS rebinding。内网集成若确有需要，使用明确授权的专用目的地规则，不机械禁止所有私网。

依据：[OWASP SSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)。

## 2. 文件 ID、存储键和客户端文件名是不同数据

Bad：文件名直接参与路径，扩展名或 Content-Type 被当作真实文件类型。

```python
return FileResponse(storage_root / client_filename)
```

Good：请求提供对象 ID，鉴权查询取得服务端生成的存储键，由存储适配器控制真实路径与资源生命周期。

<!-- example: owned-blob-python -->
```python
from uuid import UUID

async def open_owned_blob(blob_id, principal, repository, storage):
    identifier = UUID(blob_id)
    blob = await repository.find_owned_blob(identifier, principal.tenant_id)
    if blob is None:
        raise FileNotFoundError("blob not found")
    return await storage.open_read(blob.storage_key)
```

`find_owned_blob` 包含实际权限范围；storage_key 由服务端生成，不能回退到原始文件名。open_read 返回资源后，响应适配器负责关闭到真实消费结束。调用方仍需按内容类型设置安全的 Content-Disposition / 下载策略。

本地存储适配器不能只靠字符串 startswith 判断根目录；路径规范化后仍需考虑符号链接、检查与打开之间的替换、覆盖和并发创建。优先应用拥有且不受上传者修改的目录及原子创建。上传限额覆盖正文、文件、数量、临时空间与实际解压后字节。归档处理限制条目数、解压总量、单项尺寸和时间，处理绝对路径、父目录逃逸、跨平台路径、symlink / hardlink；不直接对不可信包调用无限额解压。

依据：[OWASP File Upload](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html)。

## 3. 优先使用语言 API；参数数组仍要限制工具参数语义

Bad：外部文本成为 shell 语法。

```python
subprocess.run(f"mkdir {user_directory}", shell=True)
```

Good：需要创建工作目录时，使用应用控制的父目录和服务端标识，不启动 shell。

```python
from uuid import uuid4

target = owned_work_root / uuid4().hex
target.mkdir(mode=0o700, exist_ok=False)
```

如果确实调用外部工具，用已知可执行文件与独立 argv（Java 对应 ProcessBuilder），不把用户字符串再传给 `sh -c` / `cmd /c`。参数数组不防工具选项注入；固定选项集合，验证业务参数，只有工具支持时用 `--`。输入 / 输出路径属于受控目录，限制运行时长、子进程、输出与磁盘占用；父进程超时不必然清理整个子进程树。环境变量与权限按所需最小范围，不通过命令行或日志携带凭证。

依据：[OWASP OS Command Injection Defense](https://cheatsheetseries.owasp.org/cheatsheets/OS_Command_Injection_Defense_Cheat_Sheet.html)。

## 4. 展示文本使用文本 sink，富文本另走受控解析

Bad：把用户文本解释为 HTML。

```javascript
element.innerHTML = userText;
```

Good：普通展示元素使用文本节点；React 普通文本子节点同样由框架处理转义。

```javascript
element.textContent = userText;
```

这是普通 div / span 等展示元素的策略，不适用于 script / style 的可执行内容。不要先 HTML 编码再放进 textContent 造成双重编码。富文本需求使用项目维护的 sanitizer 和明确允许策略，再交给 HTML sink；过滤后再拼未验证片段会破坏保证。URL、CSS、HTML 属性与脚本上下文分别处理，不能用一套字符串替换通吃。Cookie 认证的状态变更保留框架的 CSRF / Origin 等防护，CORS 不是授权机制。

依据：[OWASP XSS Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross_Site_Scripting_Prevention_Cheat_Sheet.html)。

## 5. 解析数据不应隐含执行代码

Bad：把不可信输入交给能构造对象或执行代码的反序列化机制。

```python
payload = pickle.loads(untrusted_bytes)
```

Good：在尺寸受控的数据格式上使用明确 schema，业务授权和状态校验继续执行。

```python
event = ExternalEvent.model_validate_json(bounded_json_bytes)
```

ExternalEvent 的版本、已知字段和兼容策略见 [防御性指南](defensive-and-observability.md)。JSON 不自动解决超大载荷、危险业务字段或权限；YAML 使用安全数据加载模式，JVM 反序列化与 XML 解析按实际库限制类型 / 外部实体。导入配置、模板和插件也属于执行边界，不因为扩展名叫“数据”就信任它。

## 6. 秘密派生值比较：使用专用比较函数，不自行逐字节提前返回

适用：协议需要比较 MAC、签名摘要或高熵秘密 Token 的地方。优先使用项目已验证的协议 / 密码库；普通业务 ID、公开枚举比较不需要这套防御。JWT / OAuth / 密码校验仍交给对应库，比较函数不能代替算法允许列表、issuer / audience / expiry、KDF 或完整签名验证。

Bad：普通相等 / 循环提前返回可能暴露匹配前缀的内容相关耗时，手写 sleep / 加随机延迟并不消除它。

```python
if expected_tag == received_tag:
    accept_webhook()
```

Good：协议适配器已把有界签名头解码为固定格式 bytes、正文限制大小；认证比较用 `hmac.compare_digest`，不要先转为普通字符串逐段比较。

<!-- example: signature-hmac-good -->
```python
import hmac

def valid_webhook_tag(secret: bytes, bounded_body: bytes, received_tag: bytes) -> bool:
    # secret 来自可信配置；签名头在边界解码为 bytes，不接收混合 str/bytes。
    expected_tag = hmac.digest(secret, bounded_body, "sha256")
    # 32字节是公开协议格式；不把内容不匹配的位置暴露出去。
    return len(received_tag) == 32 and hmac.compare_digest(expected_tag, received_tag)
```

Bad / Good（Java）：固定算法输出为字节数组，期望摘要放在第一个参数；不使用 `Arrays.equals` 或字符串 equals 代替秘密比较。

```java
// Bad
boolean valid = Arrays.equals(expectedTag, receivedTag);
// Good：expectedTag 是可信算法生成的固定长度非null值，receivedTag 已按协议解码。
boolean valid = MessageDigest.isEqual(expectedTag, receivedTag);
```

Python compare_digest 要求同类型（ASCII str 或 bytes-like）；不同长度 / 类型仍可能暴露长度或类型信息，它不是“整个认证流程恒定时间”的证明。JDK isEqual 的内容比较耗时不依赖字节值，按 API 将固定长度 expected 放前；公开格式长度可先拒绝，秘密不参与分支或错误文案。解析长度 / 格式、配置缺失、未知 key id 等属于入口或配置边界，不在内部每个调用层反复检测。

还需按协议对原始签名字节 / canonical request 计算摘要，防止中间 JSON 重序列化改变内容；时间戳、nonce、重放窗口及 key rotation 是独立责任。不能把一次成功签名校验当作任意业务操作的授权，更不能把密钥 / Token / 完整签名写日志。行为单测只能证明接受 / 拒绝语义，不能通过几次 perf_counter 测量证明没有计时侧信道。

依据：[Python hmac.compare_digest](https://docs.python.org/3/library/hmac.html#hmac.compare_digest)、[Java MessageDigest.isEqual](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/security/MessageDigest.html#isEqual(byte%5B%5D,byte%5B%5D))。

## 7. 租户、权限与资金状态：在权威存储里绑定，不信请求和缓存自报

租户来自已认证并授权的服务端上下文；URL / DTO 里的 tenant_id 不能覆盖它。DTO 证明格式合法，不能证明该用户此刻有权读写。存储查询、关联加载、批量更新 / 删除、缓存键和幂等键都保持相同租户范围；不要仅在 Controller 判断一次后交给不带租户的通用 updateById。

Bad：先全局按 ID 取数据再判断租户，或直接相信前端状态 / 普通缓存许可来扣款。

```sql
SELECT * FROM invoices WHERE id = :invoice_id;
UPDATE accounts SET balance = balance - :amount WHERE id = :account_id;
```

```python
if request.is_payable or await cache.get(f"allowed:{request.account_id}"):
    await payments.charge(request.account_id, request.amount)
```

Good：读取与写入都在数据库条件中绑定认证租户；资金前置条件与版本由同一短事务里的权威数据决定，不使用客户自报金额和可支付状态。

```sql
-- schema / 领域契约保证 amount_cny_fen > 0；不能把负金额当作一次合法扣款。
SELECT id, amount_cny_fen, source_account_id, version
FROM payment_intents
WHERE tenant_id = :authenticated_tenant AND id = :intent_id
  AND state = 'pending'
FOR UPDATE;
-- 结果不存在时按领域契约拒绝/处理重放，不执行扣款。
UPDATE accounts SET balance_cny_fen = balance_cny_fen - :stored_amount
WHERE tenant_id = :authenticated_tenant AND id = :stored_source_account
  AND balance_cny_fen >= :stored_amount;
-- 检查rowcount=1；与intent状态迁移/去重/outbox同一事务，否则整体回滚。
```

数据库唯一约束 / 外键也应体现实际租户关系，防止跨租户关联；RLS 若由项目维护，可作为另一层防线，需验证连接池复用时身份设置 / 复位，不能假设登录一次就永远正确。服务端角色、账户冻结、额度与状态是否需每次重验由业务一致性契约决定；前端只控制交互，普通缓存只能做加速或预拒绝，不能绕过资金 / 授权的权威判断。数据库事务外的真实支付仍按 [持久意图与幂等恢复](data-and-resilience.md) 执行，以上 SQL 不是完整支付系统。

验证至少覆盖相同 ID 的不同租户、批量输入混入其他租户、读取后权限 / 状态改变、缓存陈旧许可、并发版本冲突，以及前端金额与服务端意图不一致。不要把经过认证的请求等同于对每个对象授权。

## 验证选择

相关改动覆盖未知 provider、重定向和目的地策略；跨租户对象读取；路径 / 归档逃逸及限额；特殊字符与工具选项；把 HTML 载荷当普通文字展示；超大和不支持的序列化输入。没有目标运行时 / 网络隔离环境时，区分端口测试、静态检查与真正安全集成验证，不作完整防护承诺。
