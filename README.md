# DeepSeek Search / Fetch MCP

通用 Streamable HTTP 服务，使用官方 Python MCP SDK。只注册 `search` 和 `fetch`，不调用模型、不使用 sampling、不允许客户端指定上游地址。公网 URL：**https://mcp.mshushu.cn/deepseek**。路径原样保留，默认 `MCP_PATH=/deepseek`，不附加 `/mcp`。

## 当前交付状态

源码已传至上海 `/data/docker/deepseek-search-mcp`，镜像 `deepseek-search-mcp:0.1.0` 构建成功并经 inspect 确认：`sha256:61ca44b25f50b77fd62c3a8716d053b8efbcbd0dc13419bdcf2a465593978335`，运行用户 `65532:65532`。原构建最终取消，确认进程结束后才重新构建；新构建耗时13分26秒，主要耗时为 PyPI 下载约21–23kB/s，不是基础镜像拉取卡死。未改注册表或 Docker 全局配置。

Docker 容器中原有 unittest 3 项及新增 smoke 4 项均已通过（有 SDK 弃用及 Pydantic 字段警告）。测试使用隔离网络与模拟上游；没有真实 DeepSeek API 成功记录，不代表公网服务已上线。固定使用 `mcp==1.26.0`；MCP 2.0.0 不兼容此实现。

## 构建和测试

目标源码目录 `/data/docker/deepseek-search-mcp` 已存在且传输完成。全部 Docker 数据沿用服务器 `/data/dockerapp`；不更改已有容器。首次使用请先构建镜像，再执行以下测试。

```sh
cd /data/docker/deepseek-search-mcp
docker build -t deepseek-search-mcp:0.1.0 .
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges deepseek-search-mcp:0.1.0 python -m unittest discover -s tests -v
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges -v /data/docker/deepseek-search-mcp/smoke_container.py:/app/smoke_container.py:ro deepseek-search-mcp:0.1.0 python /app/smoke_container.py
docker image inspect deepseek-search-mcp:0.1.0 --format '{{.Id}}'
```

测试中的上游回复为**明确标注的 MOCK FIXTURE**，不是真实搜索结果，不产生真实计费。当前测试覆盖 SDK initialize/list_tools/call_tool、两种 profile token、未认证 GET/POST/DELETE、Host/Origin 拒绝、参数校验、错误脱敏与无重试、上游 URL 白名单。容器内真实 TCP/SDK、跨 token 身份断言、请求体/频率限制和非 root 测试已通过。公网反代及真实上游仍需单独验收；并发/超时边界尚未全面测试。

## 运行配置

由管理员另行准备 `secrets/deepseek_api_key` 和 `secrets/mcp_tokens.json`；示例 `tokens.example.json` 使用刻意无效的短占位符，启动将失败。每个 profile 使用独立随机 token，至少 32 ASCII 字符，推荐至少 32 随机字节产生的 URL-safe token。不要复用 DeepSeek API Key 作为 MCP token。

密钥只在运行时以文件挂载或环境变量注入：`DEEPSEEK_API_KEY_FILE` / `DEEPSEEK_API_KEY`，`MCP_TOKENS_FILE` / `MCP_TOKENS`。文件配置优先。容器 UID/GID 65532 必须可读密钥文件，宿主父目录限制访问。不要把真实 secrets 放入构建上下文或版本库。密钥轮换需重启新服务。空配置、短 token、重复 token 会拒绝启动。

```sh
docker compose up -d --build
```

上述是管理员部署指引，本次未执行。容器监听 `0.0.0.0:8080`，宿主仅映射 `127.0.0.1:18082`；不要直接暴露公网。若反代也在容器中，127.0.0.1 代表反代自身，应由管理员选择 Docker 共享网络/上游连接方式，不要盲目改成公网监听。

## 用户负责的 HTTPS 反代

同域其他 MCP 服务使用其他精确路径。Nginx location 示例（放入已有正确 TLS server，**本次不修改反代、DNS 或防火墙**）：

```nginx
location = /deepseek {
    proxy_pass http://127.0.0.1:18082;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 60s;
    proxy_send_timeout 60s;
    client_max_body_size 16k;
}
```

`proxy_pass` 不带尾部 URI，保留 `/deepseek`，不重写到 `/mcp`。转发 MCP 相关请求头/响应头，不剥离协议版本或认证头。不要记录 Authorization、请求体和查询内容。准确配置 Host 白名单（包括必要端口）；默认无 Origin 的 CLI 请求允许，带 Origin 的浏览器请求默认拒绝，若确实需要浏览器再显式配置精确 HTTPS Origin 白名单，不使用 `*`。

## Hermes 各 profile

`hermes-config.example.yaml` **只是示例**，管理员自行合并到各 profile `config.yaml` 的 `mcp_servers`，保持原模型配置不变。每个 profile 填自己的 Bearer token，共享同一 URL；不向 profile 分发 DeepSeek API Key。服务端身份只由 token 映射得到，忽略客户端声明的身份。Hermes 仍使用原有模型，这个 MCP 仅提供检索工具。

## 边界和费用

- Search：query 1–128 字符且非空白，count 1–50 默认10，need_content 严格布尔默认false；本版不暴露可选 ori_question/localization。
- Fetch：仅 HTTP(S)，URL 最长8192；拒绝 URL 用户名密码和明显本地/私有 IP literal、localhost 等。解析与最终取回由 DeepSeek 执行；这里没有声称能阻止上游 DNS 重绑定/重定向到私网。
- 每请求鉴权；无会话缓存或会话复用，避免跨 profile session 泄漏；全局并发8、每 profile 每分钟120个 HTTP 请求（不是费用硬上限），单进程内存限流，重启清零。
- 请求体上限16KiB；上游 connect 5s、read 30s、整体35s；上游响应最多2MiB；输出字段文本合计最多20000字符（JSON/MCP包裹有额外开销）。不会自动重试计费调用。
- 错误统一脱敏，应用/SDK日志关闭以避免验证异常带出原始查询；由反代提供不含敏感信息的状态码监控。
- 费用以官方为准：有效非空 Search/Fetch 结果人民币5元/千次；空结果/失败不计费。适配器不缓存、不实现每日费用硬限额。
- 不提供 OAuth discovery；适合管理员给可信 MCP 客户端预配置 Bearer token。

参考：https://api-docs.deepseek.com/zh-cn/guides/search
