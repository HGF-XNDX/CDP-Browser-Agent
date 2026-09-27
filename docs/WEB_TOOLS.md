# 快速联网工具与自动代理（0.5）

信息查询任务可以先 `web_search` 找地址，再 `web_fetch` 读取正文；已知 URL 则直接读取。
纯联网任务不启动浏览器。遇到动态页面、登录、访问验证或无法提取的资源，工具返回
`needs_browser`、`browser_url` 和具体状态，模型可以改用 `navigate` / `observe_browser`。
需要当前登录会话、表单、点击、下载时，可直接选择浏览器，不强制经过搜索。

这两个工具都支持 MCP 直接调用和 CLI 直接调用，不必运行项目内部模型：

```powershell
python -m cdp_browser_agent.browser --web-search "Python official documentation"
python -m cdp_browser_agent.browser --web-fetch "https://example.com"
python -m cdp_browser_agent.browser --config examples/harness-30000.json "先搜索 Python 官方文档，再读取一个官方网页，给出标题和来源"
```

MCP 工具参数：

| 工具 | 参数 | 行为 |
|---|---|---|
| `web_search` | `query`, `max_results=5`（1–10） | 标题、URL、摘要、服务来源与时间 |
| `web_fetch` | `url`, `offset=0`, `max_chars=6000`（500–12000） | 正文片段、链接、原文文件、哈希与回退状态 |

内部规划器可以用统一 `action=tool` 调用，也兼容 `action=web_search/web_fetch`；
两种写法进入同一个注册工具和参数校验，不产生另一条执行通道。
`browser_capabilities` 报告配置概况，不能代替真实联网验证。

## 免密搜索

默认 `web.search.provider=auto`：先使用 Bing 搜索 RSS；请求/解析失败时尝试 DuckDuckGo HTML。
它们是公开搜索入口，可能限流、要求验证或改变格式，不能承诺与付费搜索 API 相同的稳定性。
只要识别到明确的搜索结果结构才返回成功；网络错误、验证页面、未知 HTML 不记为“零结果”。
搜索摘要只用于发现线索，正文应再次 fetch 或在浏览器中读取。

可选 `provider=bing_rss` / `duckduckgo` 固定来源。
也保留 `searxng`（`endpoint` 填实际 `/search` 地址且启用 JSON 输出）及 `tavily`
（`api_key_env` 默认 `TAVILY_API_KEY`）适配器。默认不启用付费服务，不需要 API key。
这些适配器按服务文档实现，本轮没有使用付费服务做线上验证。

## 两个工具都自动检测代理

```json
{
  "web": {
    "enabled": true,
    "prefer_fast_path": true,
    "proxy": "auto",
    "timeout_seconds": 30,
    "max_response_bytes": 2000000,
    "artifact_dir": "../downloads/web",
    "allowed_private_hosts": [],
    "search": {"provider": "auto"}
  }
}
```

`auto` 检查 HTTPS_PROXY、ALL_PROXY、HTTP_PROXY（含小写形式），Windows 上还读取
已启用的系统手动代理，然后加入直连并去重。不扫描端口，不修改系统配置。
同一环境变量优先选择 HTTPS/ALL/HTTP 顺序；`NO_PROXY` / `no_proxy` 可使匹配目标直接连接。
HTTP(S) 和 SOCKS 代理由 HTTPX 处理。系统 PAC/WPAD 自动配置脚本暂不执行。

实际连接失败后尝试下一条线路，在整体 30 秒预算内完成；成功线路按 search/fetch 分别缓存
5 分钟，代理配置变化后失效。HTTP 403、429 或验证页面不会触发同一请求轮换代理。
自动搜索模式可以改试另一个搜索来源；验证仍需人工时交给用户。

设 `proxy: ""` 强制直连；显式代理 URL 固定线路。也可独立覆盖：

```json
{"web":{"proxy":"auto","search":{"proxy":"auto"},"fetch":{"proxy":""}}}
```

日志只记录 `network_route`、`network_attempts` 中的线路类别、HTTP 状态或异常类型，
不输出代理地址、用户名、密码或含凭据的异常字符串。不会自动继承浏览器 Cookie。

## 正文、来源和状态

HTML 提取优先使用 article/main，否则读取 body，去掉脚本、样式和常见导航；支持普通文本、JSON、XML。
不执行 JavaScript，不下载图片，不做 PDF/OCR，也不会把正文交给模型重写。
正文和原始响应分别保存为 `content.txt`、`response.bin`，`source.json` 保存来源、时间和 SHA-256。
输出路径以配置文件目录解析，未提供配置时以当前工作目录解析。

长正文通过 `next_offset` 继续读取，`total_chars` 表示提取文本长度。工具上下文预算可能进一步缩短片段，
完整提取文本仍在本地文件中。同一个运行/MCP 服务为后续片段保留最多 16 页、60 秒缓存；
`offset=0` 发起新读取。缓存过期后的片段可能来自更新后的网页，可通过哈希与时间识别。
提取的文本不等于原网页所有信息，也不证明与用户主题相关。

`success` / `empty` 是有效响应；`challenge`、`authentication_required`、
`insufficient_static_content`、`network_error` 等需要明确处理。`rate_limited` 不自动转浏览器反复请求。
`restricted_url`、`disabled`、`configuration_error` 不是改用浏览器绕过配置的理由。

默认 fetch 拒绝 URL 内凭据、私网/回环/链路本地/保留地址，每一跳重定向重新检查。
直连固定到已检查 IP 并保持原域名的 TLS 验证；经过操作方代理时保留目标域名以正确验证 TLS，
代理掌握自身的 DNS 和出口，因此代理必须可信。本功能不是操作系统网络沙箱。
开发夹具/内网站点可由操作方在 `allowed_private_hosts` 精确列出主机名，MCP 调用者不能更改此配置。

工作流的 `agent` 步骤需在 `allow_tools` 中显式加入 `web_search` / `web_fetch`；
fetch 的请求和重定向仍受该工作流 `allowed_origins` 约束。工作流本身已经打开浏览器时，
联网工具只作为额外能力，不关闭或替换既有页面。

## 官方接口参考

- [DuckDuckGo 非 JavaScript 搜索入口](https://safe.duckduckgo.com/duckduckgo-help-pages/features/non-javascript)
- [Bing RSS 搜索入口的官方说明（历史实验功能）](https://blogs.bing.com/search/2005/1/RSS-Feeds-for-Search-Results/)
- [SearXNG Search API](https://docs.searxng.org/dev/search_api.html)
- [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [HTTPCore SNI 扩展](https://www.encode.io/httpcore/extensions/)
