# 外部输入到执行边界

仅在改动触及不可信输入及其执行边界时读取，不对纯格式、纯计算或无关 UI 改动发起全面安全重构。保留框架和项目已有安全设施；下面是需接到实际端口的模式，不是通用安全库。

## 1. 固定集成按服务标识选择，不把任意用户 URL 交给内部网络

Bad：简单检查字符串前缀不约束真实目的地、重定向或网络路由。

```python
if user_url.startswith("https://"):
    return await client.get(user_url, follow_redirects=True)
```

Good：业务只访问已配置供应商时，用户只能选择有限标识；地址在可信配置中，响应作用域覆盖使用。

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

## 验证选择

相关改动覆盖未知 provider、重定向和目的地策略；跨租户对象读取；路径 / 归档逃逸及限额；特殊字符与工具选项；把 HTML 载荷当普通文字展示；超大和不支持的序列化输入。没有目标运行时 / 网络隔离环境时，区分端口测试、静态检查与真正安全集成验证，不作完整防护承诺。
