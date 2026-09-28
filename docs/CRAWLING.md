# 通用 HTTP 爬虫（0.9）

信息采集现在有三条执行路径：`web_search/web_fetch` 查找或读取少量网页；
`web_crawl` 执行静态页面的批量采集；Playwright/CDP 处理登录和动态交互。
主智能体决定采集范围、规则和异常处理方式，HTTP 爬虫按规则运行，不逐页调用模型。
处理子智能体可以接收完整记录集，再按注册方法生成需要的格式。

## 基本用法

```powershell
python -m cdp_browser_agent.browser --crawl-spec examples/crawling/example-domain.json
python -m cdp_browser_agent.browser --crawl-resume <crawl_id>
python -m cdp_browser_agent.browser --crawl-status <crawl_id>
python -m cdp_browser_agent.browser --crawl-read <crawl_id> --crawl-offset 0 --crawl-limit 4000
python -m cdp_browser_agent.browser --crawl-pause <crawl_id>
```

需要使用模型安排采集和加工时，继续使用 30000 端口的配置：

```powershell
python -m cdp_browser_agent.browser --config examples/learning-30000.json "采集指定公开网站的文章，先检查页面结构，再按标题、来源链接和正文导出结果"
```

实际任务应指定目标网址及范围。命令中的自然语言只是任务写法示例。
免模型直接爬取不需要连接 30000，也不启动 Chromium。

MCP 新增 `web_crawl`、`web_crawl_status`、`web_crawl_read`、`web_crawl_pause`，总计 25 个工具。
MCP 调用示例：

```json
{"spec":{"seed_urls":["https://example.com/"],"max_depth":0,"max_pages":1}}
```

返回 `crawl_id` 后，完成的采集结果可以直接交给：

```json
{"profile":"已注册的处理方法名","crawl_id":"返回的crawl_id"}
```

外层 MCP 调用 `browser_worker_start`；内层智能体调用 `delegate_processing`。
两者都在主机上读取完整记录集并校验哈希，不依赖父上下文里有限的来源摘要。
加工结果返回实际已校验输出的预览（最多 8 条且 4000 字符），包含总量和截断标记，
方便主智能体根据加工结果回答。预览只包含完整记录，不裁掉一半字段；大结果通过文件交付。
需要与处理 profile 的 `max_records` 一致，处理默认上限为 500；不会悄悄截断输入。
CLI 可将导出的 `records.json` 交给 `--worker-start PROFILE --processing-input PATH`。

## 采集规则

| 配置 | 含义 |
|---|---|
| `seed_urls` | 1–50 个初始 URL；默认仅允许这些 URL 的来源域（含端口） |
| `max_pages` / `max_depth` / `max_records` | 默认 20 页、深度 1、1000 条，受操作员上限约束 |
| `link_selector` | 默认 `a[href]`，所发现链接深度加一 |
| `next_selector` | 普通下一页 href 链接，保持当前深度，可在 depth=0 下分页 |
| `include_patterns` / `exclude_patterns` | 对完整 URL 使用 glob 通配匹配，作用于种子、链接和重定向 |
| `record_patterns` | 只从匹配 URL 提取记录，其他页面仍可用来发现链接 |
| `item_selector` + `fields` | 每个列表项提取多个字段；不传 item_selector 则整页提取一条 |
| `fields.<name>` | selector、attribute、required（默认 true）、url（相对链接转绝对链接） |
| `key_fields` | 按字段组合去重；同一键不同内容记为失败，不覆盖旧记录 |
| `allow_empty` | 只有明确允许空结果时才设置；默认找不到列表项就是失败 |

字段规则为 CSS、文本或属性，不执行用户脚本。未配置 fields 时保存每页完整提取正文及标题。
`web_fetch(content_format="html")` 可分片读取原始 HTML 来检查结构，CLI 对应
`--web-fetch URL --web-fetch-format html`。HTML 同样作为不可信网页内容处理。
字段 selector 在列表项内部寻找子元素；读取列表项自身属性时省略 selector 或使用 `:scope`。
例如列表项本身的 data-id 为 `{"attribute":"data-id"}`，不应写成找子元素的 `[data-id]`。
URL 保留查询参数，去除 fragment；支持 HTML base href。种子和链接均规范化去重。
本版没有 XPath、嵌套字段、Sitemap/RSS 专门发现器、附件解析或分布式并发。

固定工作流新增独立步骤，原有依赖浏览器的 `crawl` 不变：

```json
{
  "id": "collect", "type": "http_crawl",
  "spec": {"seed_urls": ["https://example.com/"], "max_depth": 0, "max_pages": 1}
}
```

后接 `{"id":"format","type":"process","input_step":"collect","profile":"方法名"}`。
完整工作流仍要配置 `schema_version/name/version/description/allowed_origins/steps`。
HTTP 采集还受外层 `allowed_origins` 限制，不能通过子工具放大采集范围。

## 运行、恢复和证据

SQLite 保存待抓取队列、已抓取 URL、robots 缓存、结果引用、任务租约、暂停请求和站点限速。
页面以事务检查点保存；新进程按 crawl_id 恢复，已完成页面不重复请求。崩溃时未确认完成的
GET 请求可能重放，不能声称网络请求 exactly-once。单次调用默认最多处理 10 页、30 秒，
`page_budget` 最多 50。达到单次预算会暂停，不代表任务完成。当前没有后台常驻调度器，
恢复需要调用者再次调用；重试退避期间查看 `next_retry_at`。

结果状态：`completed` 为所声明深度和过滤范围内队列耗尽；`paused` 为可继续；
`incomplete` 为存在失败、拒绝或总量上限导致遗漏；`failed` 为执行异常。
不会将页面限制、空选择器、登录页或验证码包装成完整成功。
终态 incomplete 的失败列表及快照保留，修正规则或重试失败 URL 应创建新任务。

每条记录保存来源 URL、请求 URL、采集时间、原始响应/正文哈希和文件引用。
JSON、JSONL、CSV 与 pages.json、manifest.json 按每次调用生成新版本，不覆盖旧交付。
CSV 会转义公式前缀。`web_crawl_read` 校验记录集快照哈希，处理交接逐条校验原始记录哈希。
来源哈希证明内容一致，不代表提取字段或模型推理语义必然正确。

默认解析 robots.txt（使用 Protego 支持通配及优先规则），遵守 Crawl-delay/Request-rate。
同一 state_dir 中的采集共享来源域限速和 Retry-After；每个任务内顺序执行。
429、网络临时失败与 5xx 只按有限次数退避重试。页面及重定向复用 web_fetch 的代理自动检测、
私网地址限制和响应大小限制。动态、登录或挑战页面单独给出 browser_fallback；
由智能体决定是否使用已授权浏览器继续，不自动绕过 robots 或站点限流。

操作员配置示例（路径相对配置文件）：

```json
{
  "crawler": {
    "enabled": true, "state_dir": "downloads/crawls", "max_pages": 200,
    "max_records": 5000, "request_delay_seconds": 0.5, "max_retries": 2,
    "run_timeout_seconds": 30, "respect_robots": true, "max_data_bytes": 50000000
  },
  "web": {"proxy": "auto", "max_response_bytes": 2000000, "timeout_seconds": 30}
}
```

工具参数不能修改操作员权限、代理、robots 开关或总量上限。采集不继承浏览器登录 cookies。
所有未完成/已完成状态、导出和证据都会保留；目前没有自动保留期和磁盘总配额管理。

## 借鉴的现有实现

2026-09-28 对照了以下官方资料，采用适合现有 Python/Playwright 项目的机制，
没有把多个完整智能体框架嵌套安装或复制其业务代码：

| 参考 | 借鉴与本项目落点 |
|---|---|
| [Browser Use](https://github.com/browser-use/browser-use) | 浏览器执行能力同时供 agent、CLI 和外层宿主使用；本项目的四个采集入口共享同一引擎 |
| [Crawl4AI 规则提取](https://docs.crawl4ai.com/extraction/no-llm-strategies/) | 规则确定后使用 CSS 批量提取，重复页面不再逐页调用模型 |
| [Crawl4AI 深度爬取](https://docs.crawl4ai.com/core/deep-crawling/) | 将深度、URL 范围、页数预算和恢复状态显式化 |
| [Crawlee 请求存储](https://crawlee.dev/js/docs/guides/request-storage) | 区分待请求与已完成队列、持久化去重和任务恢复 |
| [Scrapy Jobs](https://docs.scrapy.org/en/latest/topics/jobs.html) | 每个任务有独立状态，暂停恢复复用队列，不重新开始采集 |
| [Anthropic 长任务 harness](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | 跨上下文靠可恢复状态和可验证交付衔接；这次落地为 crawl_id 与完整数据交接，而非不断堆长提示词 |
| [Protego](https://github.com/scrapy/protego) | 直接复用已维护的 robots 解析器，网络获取、状态码和缓存由主机控制 |

这些是设计来源与工程实现依据，不是性能领先的证明。本次不声称已实现自主代码进化、
跨网站通用高成功率或无人值守生产调度。
