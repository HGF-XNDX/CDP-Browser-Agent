# CDP Browser Agent

[English](README.md) | 中文

当前文档任务实测使用 [gpt-6-luna 配置](examples/documents-gpt-6-luna.json)：
`http://localhost:15536/v1`，密钥从 `CDP_BROWSER_AGENT_API_KEY` 环境变量读取。
本轮通用修复、组件检查、真实工具访问和任务完成状态分别记录在
[恢复修复验收](docs/DOCUMENT_RECOVERY_VALIDATION.md)。
Cockpit 的请求级图片工具关闭、单步规划响应及后续实测见
[Cockpit 接口兼容记录](docs/COCKPIT_API_COMPATIBILITY.md)。

通用浏览器智能体。使用 Playwright 启动 Chromium 或连接 Chrome/Edge CDP，
使用 OpenAI-compatible Chat Completions 接口规划动作。支持内部加载 Agent Skills、
调用外部 MCP 工具，也能作为 MCP 服务或一个可分发的 Skill 被其他智能体使用。

0.10 加入借鉴 ACE 的经验学习：任务结束后自动反思、整理结构化经验，经独立样例对照回放
验证改善后才供后续任务使用。经验按网站、任务类型、模型／工具／Skill 配置隔离，支持
版本、过期、停用和回滚，覆盖浏览器规划器与处理子智能体。30000 通用配置已开启候选学习；
`examples/playbook-30000.json` 提供注册工作流内的自动回放与采用示例。
详见 [经验学习配置与实现边界](docs/PLAYBOOK_LEARNING.md)、[验收记录](docs/PLAYBOOK_VALIDATION.md)。
MCP 共 36 个工具。

需要在文档修复过程中启用 ACE 学习时，使用
[documents-ace-gpt-6-luna.json](examples/documents-ace-gpt-6-luna.json)。独立 Reflector
根据当前来源的真实失败提出建议，下一轮规划实际接收；Curator 生成的长期经验仍须
通过独立对照回放后才能供后续任务召回。新增的回放会执行配方并检查输出，评审临时
失败也有保留旧记录的有界重试入口。见 [ACE 文档学习说明](docs/ACE_DOCUMENT_LEARNING.md)。

完整文档加工使用通用工具：检查结构、解码嵌入文档、由规划器根据原文生成 CSS/XPath/
正则规则、预览校验、回放后导出 JSON。未选择的附表、注释及其他结构保留供检查。
详见 [文档工具与问题诊断流程](docs/DOCUMENTS.md)、[真实执行与验收边界](docs/DOCUMENT_VALIDATION.md)。此前的专利法专用适配器已移除：
配置好专用方法后成功，不能证明智能体能自主处理新文档。

0.9 加入基础 HTTP 爬虫：批量 URL、站内链接、CSS 字段提取、普通链接翻页、去重、
限速与退避、robots、暂停和断点恢复，导出 JSON/JSONL/CSV。重复页面采集不逐页调用模型。
记录集可通过 crawl_id 完整交给处理子智能体；固定工作流新增 http_crawl 步骤，
动态交互继续使用浏览器。详见 [爬虫使用与现有框架借鉴](docs/CRAWLING.md)、
[爬虫与加工验收](docs/CRAWLER_VALIDATION.md)。

0.8 加入可持续加工子会话：同一份材料可以接受后续反馈、分轮修订、跨进程恢复和取消；
主任务完成前可以按配置核验加工结果。成功修订产生候选经验，只有在独立样例上通过
原方法与候选方法的对照回放，才会被后续任务采用。CLI 和 MCP 共用持久化状态。
详见 [持续子会话与经验回放](docs/CONTINUING_WORKERS.md)、[实测验收](docs/WORKER_VALIDATION.md)。

0.7 自动检测模型实际上下文容量，统一主规划器、记忆和处理子智能体的预算；大工具结果完整留存并支持引用检索，压缩视图可恢复。30000 示例自动识别 262,144 tokens，显式设置的小窗口仍然生效。详见 [上下文与证据管理](docs/CONTEXT_HARNESS.md)。

0.6 增加独立的数据处理子智能体：通过方法、Skill 和输出 Schema 定义加工过程，
生成 JSON/CSV/Markdown；普通任务可持久化恢复，支持授权自主决定、等待超时继续，
并加入证据关联的反思、上下文恢复与经过验证的跨任务经验。
详见 [处理与自主决策](docs/PROCESSING_AUTONOMY.md)、[底层与现代 Harness 对照](docs/HARNESS_EVOLUTION.md)、
[本轮验收](docs/HARNESS_VALIDATION.md)。

```powershell
.\.venv\Scripts\python.exe -m cdp_browser_agent.browser --config examples/harness-30000.json --workflow collect-and-process
```

0.5 新增 `web_search` / `web_fetch`：默认免密搜索、正文读取、两个工具的自动代理检测，
以及按需启动浏览器。公开信息先快速检索/读取，动态页面和交互任务再交给浏览器。
配置及使用见 [快速联网工具](docs/WEB_TOOLS.md)。

0.4 已加入固定网站工作流：页面准备、宿主核验、列表/详情提取、翻页、断点恢复、
原始快照和增量比较。站点字段保存在工作流配置里，业务知识通过 Skill 和外部工具接入。
使用见 [工作流指南](docs/WORKFLOWS.md)，实测结果见 [工作流验收](docs/WORKFLOW_VALIDATION.md)。
此前改动与兼容性说明见 [升级记录](docs/UPGRADE.md)。

```powershell
.\.venv\Scripts\python.exe -m cdp_browser_agent.browser --config examples/harness-30000.json --list-workflows
.\.venv\Scripts\python.exe -m cdp_browser_agent.browser --config examples/harness-30000.json --workflow example-domain
```

工作流通过 `workflows.paths` 注册，支持 CLI、Python 和四个 MCP 工具调用。
知识产权站点示例单独使用 `examples/ip-collection-30000.json`，不会由通用配置默认加载。
其 `ipc-judgments-sample` 已真实采集 5 篇正文；`wenshu-search` 只负责检索准备，
当前未登录测试遇到登录页/空框架，尚未完成文书网正文采集。

## 安装与运行

Python 3.10+。仓库已经下载后，在仓库目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m cdp_browser_agent.browser `
  --base-url http://127.0.0.1:8080/v1 --model local-model --headless `
  --max-steps 20 "打开 https://example.com 并概括页面内容"
```

`base-url` 和 `model` 要换成实际服务。包不会自动部署大模型。
默认提供空白浏览器页面，不强制前往特定搜索引擎。
CLI 的 stdout 只输出结果 JSON，诊断信息写入 stderr。
安装后也可使用 `browser-agent`、`cdp-browser-agent` 两个命令。

## 让智能体加载 Skill

支持包含 YAML frontmatter 的 `SKILL.md`。路径既可以是某个 Skill 目录，也可以是多个 Skill 的父目录。
启动时提供名称和描述；模型选择 `skill_load` 后正文进入上下文，`skill_read` 按需读取参考资料。

```powershell
.\.venv\Scripts\python.exe -m cdp_browser_agent.browser --config examples/harness.json --list-skills
.\.venv\Scripts\python.exe -m cdp_browser_agent.browser `
  --config examples/harness.json --base-url http://127.0.0.1:8080/v1 `
  "提取 https://example.com 的页面标题和主要说明，并标注来源"
```

也可以追加 `--skill-path D:/my-skills --skill my-skill`。
`harness.active_skills` 指定预加载的 Skill；未预加载的仍可由模型按需选择。
支持 YAML 标量、多行描述、参考资料分页读取、重复名称检测、资源目录边界检查和正文预算。
文本资源包括脚本源码，但本版本不会自动执行 Skill 脚本；执行能力应由明确配置的 MCP 工具
或 Python `Tool` 处理。`allowed-tools` 元数据不会自动授予权限。

## 接入外部 MCP / Python 能力

[完整本地示例](examples/harness-with-mcp.json) 接入一个文本处理 MCP 服务。
示例的 Python 路径适用于本仓库 Windows `.venv`，其他环境修改 `command`。
模型可发现并调用 `mcp.text.normalize_text`。
可选 MCP 服务离线只标记该服务不可用，其他工具可以继续工作；设置 `required: true` 才会阻断运行。

```json
{
  "harness": {
    "mcp_servers": {
      "text": {
        "transport": "stdio",
        "command": "../.venv/Scripts/python.exe",
        "args": ["toolbox.py"],
        "allow_tools": ["normalize_text"]
      },
      "remote": {
        "transport": "streamable-http",
        "url": "http://127.0.0.1:9000/mcp",
        "allow_tools": ["lookup"],
        "headers_from_env": {"Authorization": "LOOKUP_AUTHORIZATION"}
      }
    }
  }
}
```

以上 remote 是配置格式示例，需要有实际服务才启用。
环境变量 `LOOKUP_AUTHORIZATION` 的值应包含服务要求的完整授权头，例如 `Bearer ...`。
stdio 可以用 `env_from: ["SERVICE_TOKEN"]` 显式转交必要的环境变量，也支持 `env` 对象。
不支持通配符授权；`allow_tools: []` 禁用该服务，缺少列表则启动失败。
工具名称带服务器命名空间，模型先 `tool_list`、`tool_describe`，再用 Schema 合法的参数调用。
工具超时不会自动重试，以免重复产生副作用。

Python 扩展可以直接注册异步函数，无需单独服务：

```python
import asyncio
from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.runner import run_browser_agent
from cdp_browser_agent.harness import Tool

async def uppercase(text: str):
    return {"text": text.upper()}

async def main():
    config = browser_agent_default_config()
    config["browser"]["headless"] = True
    custom = Tool("uppercase", "Uppercase extracted text", {
        "type": "object", "properties": {"text": {"type": "string"}},
        "required": ["text"], "additionalProperties": False
    }, uppercase)
    print(await run_browser_agent("打开 https://example.com，提取标题并转为大写", config, tools=[custom]))

asyncio.run(main())
```

## 作为 MCP 服务

```powershell
.\.venv\Scripts\python.exe -m cdp_browser_agent.mcp_server --config examples/harness.json
```

客户端配置示例，替换为实际绝对路径：

```json
{
  "mcpServers": {
    "cdp-browser-agent": {
      "command": "D:/浏览器智能体/CDP-Browser-Agent/.venv/Scripts/python.exe",
      "args": ["-m", "cdp_browser_agent.mcp_server", "--config", "D:/浏览器智能体/CDP-Browser-Agent/examples/harness.json"]
    }
  }
}
```

暴露 `browser_capabilities()`、`browser_task(task, max_steps?)`、`web_search`、`web_fetch`
及四个 `browser_workflow_*` / `browser_workflows` 工作流工具。两个 web 工具可独立使用，不调用模型或启动浏览器。
前者只检查配置和 Skill 目录，不连接模型或浏览器。
后者可降低部署配置的步骤上限。模型、密钥、浏览器、目录及外部 MCP 均由启动配置固定，
工具调用者不能传 `config_path` 或更换服务地址。
同一 MCP 实例串行处理浏览器任务；忙时返回 `busy`。

HTTP 模式：

```powershell
.\.venv\Scripts\python.exe -m cdp_browser_agent.mcp_server `
  --config examples/harness.json --transport streamable-http --host 127.0.0.1 --port 8000
```

连接地址为 `http://127.0.0.1:8000/mcp`。内置启动器仅允许回环绑定；跨机器服务需要自行配置鉴权部署。

## 作为 Skill 分发

内置 Skill 位于 [cdp_browser_agent/skills/cdp-browser-agent/SKILL.md](cdp_browser_agent/skills/cdp-browser-agent/SKILL.md)，
wheel 安装包包含该文件。导出到项目的 Skill 目录：

```powershell
.\.venv\Scripts\python.exe -m cdp_browser_agent.browser --export-skill .agents/skills
```

导出拒绝覆盖已有自定义 Skill。复制到支持 Agent Skills 的宿主后可由宿主发现。
该 Skill 调用 MCP 或 CLI，仍需要本项目的模型配置；不会自动复用外层智能体的模型凭证。

## 运行状态与边界

| status | 含义 |
|---|---|
| completed | 规划器报告完成；`completion_basis=model_reported`，可用来源/文件/日志复核 |
| incomplete / blocked | 规划器明确报告未完成或受到能力限制 |
| needs_input | return 策略需要用户介入，或工作流存在无法解决的访问/重放问题 |
| waiting_input | wait 策略正在等待实际答复；超时后按自主策略继续 |
| max_steps | 到达步骤上限，未验证任务完成 |
| stalled | 相同页面/工具结果反复出现而无新进展 |
| failed | 模型反复失败或浏览器运行失败 |
| timeout | 到达整轮时限；工具可能已经产生副作用，先核对后重试 |
| busy | 当前 MCP 实例有其他任务正在运行 |

日志为独立 UUID 标识的 JSONL，记录观察、动作、结果和终止状态；默认保存到工作目录下的 `logs/browser-agent`。
下载文件、页面保存和日志使用本地磁盘，可能包含任务内容。相对配置路径以 JSON 配置文件所在目录解析。
模型密钥可使用 `model.apiKeyEnv` 指定环境变量名。
默认关闭二次策略评审、模型记忆摘要和跨任务站点记忆，按需开启可控成本的辅助功能。
新的程序性经验库与旧站点记忆分开：候选策略至少经过两次独立宿主验收才参与召回，
普通模型自行报告完成不会晋升经验。`intervention.mode` 默认 auto，也可配置 wait 或 return。

运行结束会关闭本次启动的 Chromium；连接已有 CDP 时仅断开连接。
普通任务已有跨进程状态恢复与浏览器存储快照，不恢复活页 DOM/JS 堆；仍没有多租户浏览器隔离，
也没有自动执行任意 Skill 脚本的主机沙箱。新增 MCP 工具 `browser_process`、`browser_task_resume`、
`browser_task_status`、`browser_task_respond`；当前 MCP 共 36 个工具，覆盖浏览器、网页、
工作流、爬取、加工、文档和经验学习能力。
支持原生下拉框、复选框及基本 iframe 的观察与操作；复杂嵌套/跨域 frame、Canvas、文件上传尚未完成专项验证。
0.3 的内部 `done` 动作必须显式填写 `outcome=completed|incomplete|blocked`；旧自定义规划器需要更新。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m build --no-isolation
.\.venv\Scripts\python.exe -m twine check dist/*
```

测试包括真实 stdio MCP、Streamable HTTP，以及本地网页上的真实 Chromium：Skill 加载/参考读取、
外部工具调用、输入、真实键盘操作、观察和页面保存。端到端测试使用本地模型协议桩，证明接线和执行路径，
不表示开放网站任务成功率或性能已完成评测。

另已使用本地 30000 端口真实模型完成六任务前后对比，独立验收由 4/6 到 6/6，
并通过外部 MCP → CDP → 真实模型的完整调用测试。见 [实测与底层重构审查](docs/LIVE_TEST_30000.md)。
现成配置为 [harness-30000.json](examples/harness-30000.json)，不覆盖默认服务配置。
