# 采集、处理、恢复与自主决策（0.6）

浏览器负责找到并收集证据，独立数据处理子智能体按操作者注册的方法整理数据。
网页来源、处理输入、模型原始输出、校验失败、最终交付分别保存，可追溯和恢复。
核心保持通用；站点字段、业务说明和输出格式放在 Workflow、Processing Profile 或 Skill 中。

## 运行完整示例

仓库环境已经安装并有 30000 端口的 OpenAI-compatible 模型时：

```powershell
python -m cdp_browser_agent.browser --config examples/harness-30000.json --workflow collect-and-process
python -m cdp_browser_agent.browser --config examples/harness-30000.json "采集 https://example.com，用 page-facts 子智能体处理并交付文件"
```

前者是确定性 `fetch → process` 工作流；后者由主智能体选择 `web_fetch` 和
`delegate_processing`。无需浏览器交互的网页保持快速路径，动态页可回退浏览器。
示例只使用通用网页，不默认加载法律业务模板。

工作流步骤现在支持 `search / fetch / navigate / agent / crawl / process`。
`fetch` 可指定 `urls`，或使用此前 `search` 步骤的 `input_step`；`process`
指定此前步骤的 `input_step` 和已注册 `profile`。多个 process 步骤可以串联。
搜索摘要属于线索，不直接计作采集正文。完整规范见 [workflow.schema.json](workflow.schema.json)。

## 定义处理方法

通过 `processing.paths` 注册 JSON 文件或目录。配置路径相对配置文件解析。
完整实例：[page-facts.json](../examples/processing/page-facts.json)。最小方法：

```json
{
  "name": "page-summary",
  "mode": "llm",
  "instructions": "只根据输入正文生成中文摘要；缺失的信息明确说明。evidence 为 summary 提供连续原文引文。",
  "skills": [],
  "output_schema": {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": false
  },
  "evidence_fields": ["summary"],
  "formats": ["json", "csv", "markdown"],
  "max_input_chars": 60000,
  "max_records": 500,
  "max_repairs": 1,
  "timeout_seconds": 300
}
```

`skills` 引用 `harness.skill_paths` 中的名称，处理器把这些 Skill 的正文加入独立上下文。
可用 profile 的 `model` 覆盖默认模型配置，例如为采集与处理选择不同模型；
凭证建议用 `apiKeyEnv`，冻结方法文件不写入 `apiKey`。
`mode: mapping` 配合 `field_map: {"输出字段": "输入.字段"}` 做无模型的确定性字段转换。

每条记录单独处理，默认串行以适应本地模型。模型收到方法、相关 Skill、输出 Schema、
当前记录、必需引文字段和上一次校验错误；不收到主智能体的全部浏览历史，也没有浏览器、
Shell、外部 MCP 工具或递归派生能力。这里的“子智能体”是独立上下文、有受限修复循环的
处理 worker，并非可自由执行任意代码的通用子代理。

处理器核验 JSON Schema、字段类型和必需项，以及引文是否真实出现在输入值中。
一次失败最多修复 `max_repairs` 次。`require_evidence` 默认 true；可按业务显式关闭。
这些检查不证明摘要含义完全正确，因此结果始终包含 `semantic_accuracy_verified: false`。
需要专业语义验收时，应另行接入业务核验器。

`max_input_chars` 覆盖输入、方法、Schema、Skill 和修复空间，超限直接失败，不静默截断。
LLM worker 另外根据模型上下文长度和保留输出空间估算 token 预算，中文长文本超限也会明确失败。
`timeout_seconds` 是整个处理作业上限；普通智能体的工具时限和整轮时限仍可更早终止它。
长作业应使用工作流，并按预计记录数配置时限，或显式拆分输入。

普通任务的 `delegate_processing` 使用本轮已收集来源。静态抓取的完整正文通过保存文件和
SHA-256 核验读取；浏览器普通观察只有 `observed_excerpt` 范围，不能声称全文。
完整批量采集优先使用 `crawl/fetch → process` 工作流。MCP 直接处理的输入由调用者提供，
引文校验只核对输入，不为调用者填写的 source_url 背书。

## 输出和恢复

每个处理目录包含：

- `method.json`：方法、Skill、模型配置的冻结快照及 method_hash。
- `items/*.input.json`：原始输入；`items/*.json`：校验结果及来源身份。
- `attempts/<id>/*.json`：每次调用的原始响应、错误和耗时。
- `validated-records.json`：规范化的有效结果，供后续步骤使用。
- `processed.json / processed.csv / processed.md`：所选交付格式。
- `failures.json / result.json`：失败清单和汇总；失败项不混入有效结果。

JSON 保留引文和来源；CSV/Markdown 适合阅读，包含来源 URL 和记录身份。
CSV 对可能被表格软件当作公式的字符串作转义。Schema/mapping 校验与语义核验是不同层级。
恢复时相同输入、相同方法的成功记录直接复用，只重跑失败项；方法、模型或 Skill 改变必须
开启新处理目录。中断时已完成的逐条回执保留，即使尚未写最终汇总。

`fetch.on_error: continue` 会处理已有成功来源，但整个工作流保留 incomplete；恢复会回到
失败的采集步骤，跳过已完成网址，并复用处理成功项。crawl 增加详情页级检查点，某个详情
失败后不会重取同页已保存的详情。新 fetch 数据集尚无跨运行 new/changed 增量分类；原 crawl
的增量比较仍保留。

普通任务现在保存 SQLite 状态和独占租约，包括原始任务、计划、历史、来源、记忆、决策、
反思、激活 Skill 和未确定动作。CLI：

```powershell
python -m cdp_browser_agent.browser --config examples/harness-30000.json --task-status
python -m cdp_browser_agent.browser --config examples/harness-30000.json --resume-task RUN_ID
python -m cdp_browser_agent.browser --config examples/harness-30000.json --resume-task RUN_ID --user-input "使用已采集的来源继续"
```

恢复需使用相同操作配置（允许改变 max_steps，密钥值不参与指纹），激活 Skill 的正文也必须
保持不变。正在运行的租约阻止第二个执行者重复操作。崩溃后租约会过期，完成/正常退出立即释放。
运行时每次动作和终止时保存浏览器 cookies、localStorage、IndexedDB；启动模式可载入
`browser.storage_state` 或 `browser.auth_state_path`。后者也可作为跨任务登录快照路径。
这不恢复 DOM、JS 堆、未提交表单或 sessionStorage，也不保证登录永不失效；CDP 优先使用
已有浏览器的真实会话。状态目录含任务和登录信息，不应当作公开交付文件。

## 人工介入策略

```json
{"intervention": {"mode": "auto", "wait_seconds": 120, "max_auto_decisions": 3}}
```

| mode | 行为 |
|---|---|
| auto（默认） | ask_user 后立即回到模型，让其在任务范围内选择方案并说明假设 |
| wait | 持久化问题，最多等待 wait_seconds；真实答复优先，超时进入自主分析 |
| return | 返回 needs_input，保留检查点供以后恢复 |

每次决策记录问题、原因和实际答复；等待超时记录 `wait_timeout`，不会伪造用户已经批准。
自动决策可以选替代来源、可逆操作、交付已核实的部分结果；缺少可行方案时结束 incomplete/blocked。
它不能制造账号、验证码或缺失事实。超过 max_auto_decisions 后停止重复追问，保留已完成工作。
等待计入整轮 deadline；请令整轮时限大于等待时间与后续处理时间之和。

实际等待中的问题通过独立 CLI/MCP 查询、响应，不需要抢占浏览器任务锁：

```powershell
python -m cdp_browser_agent.browser --config CONFIG.json --task-status RUN_ID
python -m cdp_browser_agent.browser --config CONFIG.json --task-respond RUN_ID --request-id REQUEST_ID --user-input "实际答复"
```

`browser_task_status`、`browser_task_respond` 提供对应 MCP 接口；已过期或 ID 不匹配的响应被拒绝。
普通停止任务用 `browser_task_resume`。恢复时存在未确认动作，先把不确定性交给规划器检查，
不自动重放旧动作。工作流中断的 agent 步骤先运行宿主检查；已达到目标则推进，auto 模式下
效果仍无法确认则自主保留部分结果；显式 `retry_uncertain_step` 才允许重做该步骤。

MCP 共 12 个工具：原有能力、任务、四个工作流工具、两个快速联网工具，加上
`browser_process / browser_task_resume / browser_task_status / browser_task_respond`。
CLI 可用 `--process-profile NAME --processing-input records.json --processing-path profiles` 直接处理。
