# 固定网站工作流（0.4）

工作流将固定的访问、检索准备、字段提取、翻页和核验规则保存为 JSON。
模型只负责需要理解页面的 `agent` 步骤；正文由浏览器 DOM 提取，不由模型补写。
行业规则属于站点工作流，浏览器内核不包含行业字段或业务推断。

## 使用

```powershell
python -m cdp_browser_agent.browser --config examples/harness-30000.json --list-workflows
python -m cdp_browser_agent.browser --config examples/harness-30000.json --workflow example-domain
python -m cdp_browser_agent.browser --config examples/ip-collection-30000.json --workflow ipc-judgments-sample
```

`workflows.paths` 注册 JSON 文件或目录，`workflows.state_dir` 指定 SQLite 和输出目录。
相对路径以配置文件目录为基准。可以用 `--workflow-path` 追加目录。
参数通过 `--workflow-params '{"query":"关键词"}'` 传入；Windows 调用者也可使用 Python API 避免 shell 引号差异。

```python
from cdp_browser_agent.workflows.runner import run_workflow
result = await run_workflow("demo-catalog", config, {"base_url": fixture_url}, page_budget=1)
result = await run_workflow("demo-catalog", config, resume_run_id=result["run_id"])
```

MCP 提供 `browser_workflows`、`browser_workflow_run`、`browser_workflow_status`、
`browser_workflow_pause`。只有已配置的工作流名称可以执行；调用者不能借工具参数更换模型端点或任意指定配置路径。
暂停请求在下一个检查点边界生效，不能立即打断正在进行的页面加载。

## 定义

参见 [JSON Schema](workflow.schema.json) 和 [完整演示](../examples/workflows/demo-catalog.json)。

- `navigate`：访问固定地址，可等待指定元素。
- `agent`：自然语言指导页面准备；必须有 `checks`，支持 URL 前缀、元素可见、文本包含。
  模型声称完成后，宿主再次核验；失败则继续执行，不能直接算成功。
- `crawl`：`item_selector` 选出行，`fields` 指定文本/属性；`follow` 进入详情页提取正文。
  `key_fields` 指定去重键，`next_selector` 指定唯一的下一页 href 链接。

参数使用受限 JSON Schema（标量值，无远程引用）；`${name}` 替换文本，
`${name:urlencode}` 用于 URL 查询值。必填字段为空时失败，不猜测缺失值。
`empty_selector` 必须真实可见才能将零行认定为空结果，登录页/空白框架不算空结果。

`max_pages`、`max_records` 是边界。默认到上限标记 `incomplete`；只有明确设置
`on_limit: complete` 的样本任务才完成，并报告 `coverage: bounded`，不代表全站采完。
`min_records` 对整个工作流做数量核验。`host_verified` 仅证明配置的字段、页面和数量检查通过，
不自动证明语义相关性、历史完整性或正文中图片内容已提取。

## 持久化与重复运行

每次新运行有独立 run ID，保存冻结定义与参数。每页数据和下一页游标在 SQLite 中原子提交。
恢复运行使用 `--resume-run ID`；修改定义/参数后需要新建运行。
`--page-budget 1` 可用于逐页试采，`--workflow-status ID` 查进度，`--pause-workflow ID` 请求暂停。

同一原始定义和参数最近一次完成的运行是增量基线；输出 `new`、`changed`、`unchanged`。
未完成的运行不充当基线，也不从缺失条目推断删除。稳定去重键比标题或列表顺序更可靠。
同一次运行发现同键不同内容会失败，避免静默覆盖。

每个运行目录包含：

- `records.jsonl`：原始字段、来源 URL、采集时间、记录键与内容哈希。
- `changes.jsonl`：新增和变化记录。
- `pages.json`、`workflow.json`、`result.json`：覆盖范围、冻结规则和执行结果。
- `snapshots/<sha256>.html`：采集时的 DOM 快照，和记录中的页面哈希相对应。
- `events.jsonl`、`agent-events/`：步骤和模型动作记录。

## 当前边界

链接式翻页已实现；按钮式翻页、POST 查询、无限滚动仍需专门步骤支持，不能把 `javascript:;`
当作下一页 URL。`crawl` 会重新访问列表 URL，因此只存在内存中的筛选条件不适合直接接入。
文书网的 JavaScript 翻页尚未验证，当前 `wenshu-search` 只准备并核验结果列表。

检查点保存采集进度，不保存浏览器登录状态。`launch` 模式退出关闭浏览器；需要人工登录的站点
应使用用户可见的独立 CDP 浏览器和持久配置目录，然后复用其会话。
已提交页不会重复采集；中途失败的未提交页及其详情只读访问可能重做。
可能产生副作用的 `agent` 步骤默认不自动重放：检查后使用 `--retry-uncertain-step`，
只有明确可重放的只读步骤才设置 `replay_safe: true`。

单 run ID 有数据库租约防止并发写入；进程崩溃后须等租约过期（本次超时预算加 60 秒）。
同一 MCP 服务内浏览器任务串行，状态/暂停调用不受该锁阻塞。
`allowed_origins` 限制本流程导航，不是操作系统或子资源的网络沙箱；外部工具须另外配置白名单。
工具返回的是服务器本地输出路径，跨主机访问还需要独立的文件传输通道。
当前没有内置定时调度器；重复调用产生独立运行，可由调用方按明确的频率调度。
