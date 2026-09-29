# 三国专利法采集：工具与流程验收

日期：2026-09-30。范围由用户确定为中国、美国、日本各一部专利法。
每部法律一个 JSON 对象，`articles` 数组中每条一个元素。原文不翻译、不由模型重写。

## 来源、版本和交付范围

| 国家 | 官方来源 | 版本 | JSON 条目 |
|---|---|---|---|
| 中国 | [国家知识产权局](https://www.cnipa.gov.cn/art/2020/11/23/art_97_155167.html) | 2020 年修正，修正日期 2020-10-17 | 82 条；171 个正文段落 |
| 美国 | [GovInfo Title 35](https://www.govinfo.gov/content/pkg/USCODE-2024-title35/html/USCODE-2024-title35.htm) | 2024 Main Edition，源文件标明法律更新截至 2025-01-06 | 176 条目：168 个正文条目、7 个废止标记、1 个改号标记 |
| 日本 | [e-Gov API v2](https://laws.e-gov.go.jp/api/2/law_data/334AC0000000121?asof=2026-09-30&law_full_text_format=xml) | 按 2026-09-30 查询，返回 2026-06-24 生效的修订版本 | 464 条目：本则 302、附则 162；另保留 71 组附则及 1 份附表 |

合计 **722 个条文条目**。删除、废止、改号条目有明确状态，不计作普通有效正文。
美国原文 175 个 section 标题，其中一个将 §§155、155A 合并列出，导出为两个独立元素，
并保留共同来源标记。日本附则重复条号以组号形成不同 ID；没有 Article 节点的附则段落
仍保存在 `supplementary_provisions`。附表的行列结构保存在 `appendices`。

本次美国 uscode.house.gov 及其分发入口发生连接超时，项目浏览器回退也未能连接。
因此采用可访问的 GovInfo 官方年度版本，**不将其标为 2026 年最新整编文本**。
日本排除了 API 中尚未施行的未来版本。附则 `Extract=true` 表示官方来源所载节录，
本交付不是所有历次修正法的全文合集。

## 未修改版本的实跑

原始代码为 `0e7d732`。使用端口 30000 的真实模型执行中国专利法采集，4 次模型调用：

1. `web_fetch` 返回 `unsupported_encoding`，因为中国官方页面即使收到
   `Accept-Encoding: identity` 仍返回 gzip。
2. 智能体正确调用 `navigate` 回退浏览器并读取官方页面。
3. 加载页面采集 Skill 后，识别到现有方法不能完整逐条导出 JSON。
4. 返回 `incomplete`，没有谎称已生成 JSON 文件。

原始记录保留在 `logs/patent-laws-20260930/probe/baseline-agent.json`。
页面已读取是观察证据；模型自述“包含全部 82 条”本身不是完整性证明。

## 问题分类与修正

| 问题 | 分类 | 处理 |
|---|---|---|
| gzip 正常网页被拒绝，额外启动浏览器 | 通用下载工具限制 | 有界解压 gzip/deflate；同时限制传输量和解压后体积，拒绝损坏、截断及尾随数据；保留编码前后文件和哈希 |
| 没有适配整篇法律的拆分、序列化方法 | 已配置工具/方法缺口 | 增加可选的外部 MCP 采集方法，不向通用 planner/策略层硬编码国家、法律或条号 |
| 大份 XML/HTML 可能被分页复制进模型 | 流程选择问题 | 通用规划提示明确先发现合适方法；模型只调度，后端按保存的完整来源解析，再交给 mapping worker |
| 目录、历史注释、附则同号、未来版本混淆 | 领域方法问题 | 用官方 DOM/XML 边界、日期与作用域处理，历史注释与正文分开保存 |
| 只核对 Article 数量，会遗漏附表 | 验收流程缺口 | 增加完整 XML 顶层结构核对和独立附表数量/内容检查 |

完整来源保留在磁盘，不通过 parent preview 传递；正文转换没有 LLM 输出长度瓶颈。
来源哈希、唯一 ID、条文覆盖、加工前后对象逐项相等，以及恢复后文件哈希分别检查。
加工 worker 的 method/input/result 记录保留在本地交付目录的 `workers` 下。

## 真实智能体 + 外部 MCP 验收

采用 `examples/patent-laws-30000.json` 的可选 Skill 和 MCP 配置。
测试脚本只发送任务，**下载和导出调用由真实规划器选择并通过 stdio MCP 执行**。
脚本随后检查真实文件并执行恢复验证。

最终记录：`logs/live-patent-laws/20260930-complete/receipt.json`。
公开机器摘要：[patent-laws-20260930.json](validation/patent-laws-20260930.json)。

- 14/14 检查通过；主智能体 11 次模型调用。
- CN、US、JP 均经 `patent_source_fetch`、`patent_articles_export`、最终 status 核验。
- 三个确定性加工 worker 的正文生成调用数均为 0。
- 全程使用 HTTP，无需启动浏览器；基线已单独验证实际浏览器回退。
- 三个任务恢复均复用同一 worker，JSON 输出哈希不变。
- 原始源文件、每条正文哈希、附表，以及实测源码快照均核对。

第一次扩展方法实跑 `20260930-first` 的 13 项条文检查通过，但独立结构复核发现漏掉
日本附表，因此没有采纳该轮交付。失败判定另存 `post-run-audit.json`，原始结果保留。
补充解析和检查后使用新目录重新下载、执行，最终通过上述 14 项检查。

## 复现与配置

在仓库根目录、安装开发依赖并准备好端口 30000 的模型服务后运行：

```powershell
.venv/Scripts/python.exe -X utf8 scripts/live_patent_laws_eval.py --tag my-new-run --as-of 2026-09-30
```

每次使用新 tag，结果在 `deliveries/patent-laws/<tag>`；日志在
`logs/live-patent-laws/<tag>`。实测脚本使用当前 Python 解释器，按现有环境变量名向子进程
显式传递代理配置，不将代理值写入记录。示例配置中的 Python 路径适用于 Windows；
其他系统修改 MCP `command`。直接使用配置时，若需要环境代理，应设置已有变量的
`env_from`；系统代理仍由联网工具的 `auto` 策略检测。

可选外部服务单独启动：

```powershell
.venv/Scripts/python.exe -m examples.patent_laws.collector --output deliveries/patent-laws/new-collection --as-of 2026-09-30
```

该命令启动 stdio MCP，不是自动批量采集。宿主调用三个明确注册的工具；无需通用 shell
或任意文件写入权限。这个适配器属于仓库示例/源码包，不是 wheel 内的内置法律业务。
换成其他法律或站点，需要提供相应的来源与解析方法，而不是只换任务提示词。

## 验证的边界

本次证明三份指定官方快照的下载、结构化拆分、完整来源传递、MCP 调度和恢复可执行。
结构与哈希检查不等同于法律解释、效力判断或核对所有后续修法。本次没有训练模型、
评价广泛法律任务的泛化能力，也没有把人工指定的方法说成模型自动学会了全部法条解析。
