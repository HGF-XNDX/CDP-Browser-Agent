# 0.7：上下文容量、原始证据与可恢复压缩

本轮落实 DeepSeek Harness 对照研究中与当前 Python 浏览器智能体最直接相关的部分：
统一预算、完整结果留存，以及在原始证据之上建立可恢复的模型视图。
保留现有 Playwright/CDP、工作流、Skill 和 MCP 接口，不引入另一套嵌套运行时。

## 容量发现与统一预算

`context_budget.py` 的 `ContextBudget` 供 planner、BrowserAgentMemory、ProcessingEngine
和模型请求的最终检查共同使用。

1. llama.cpp 优先读取 `/props.default_generation_settings.n_ctx`，并校验显式模型名。
2. 不可用时读取 `/v1/models`，匹配所选模型的 `meta.n_ctx` 或 `context_window`。
   其他兼容服务只走 models，不探测专有 props；不采用 `n_ctx_train`。
3. 成功发现的进程内缓存为 60 秒，失败/未报告容量的缓存为 5 秒。
   缓存按服务、模型、provider、凭证摘要及代理环境策略区分，不记录凭证明文。
4. 服务容量未知时，采用显式配置；完全未配置时保守回退 32768。

默认 `agent.context_window_tokens=null` 表示自动。`model.contextWindowTokens` 和
`agent.context_window_tokens` 中所有显式限制均参与取最小值；已探测到的服务容量也是上限。
因此用户显式设置 32768 时，不会被自动发现的 262144 覆盖。

```json
{
  "model": {"baseUrl": "http://127.0.0.1:30000/v1", "maxTokens": 1536},
  "agent": {
    "context_window_tokens": null,
    "reserved_output_tokens": null,
    "prompt_budget_ratio": 0.85
  }
}
```

输出预留 = `max(maxTokens, reserved_output_tokens 或 0)`；
输入预算 = `floor((有效窗口 - 输出预留) × prompt_budget_ratio)`。
30000 服务为 262144 且 maxTokens=1536 时，默认输入预算为 **221516 tokens**。
这是容量预算，不是每步都填满的目标。原有页面预览、记忆条数、工具结果大小和
处理 profile 的 `max_input_chars` 仍独立限制单次信息量。

估计器对中文/日文/韩文分别计数，系统提示和消息结构也计入预算。
服务返回的 usage 只能收紧同一路由的估计；不同 worker 模型不相互污染。
图像使用保守预留，避免把 base64 按普通文本计数。估计不等于原生 tokenizer；
容量发现不证明接近 262K 时的质量、速度或完整长文理解能力。

## 完整结果与引用回读

`ArtifactStore` 在结果超过工具预览上限前保存完整规范化 JSON，返回 SHA-256 ID、字节数
和字符数。同内容去重、读取校验哈希，文件留在所属任务目录。MCP 的非文本内容先保存
原始序列化返回值，模型只看到文本和引用，不直接吞入二进制/base64。
浏览器完整 observation 同样留存；处理子智能体可读取其完整已观察文本，而非仅 6000 字摘要。
这仍只代表浏览器实际观察到的内容，不保证站点全部文档已经加载。

智能体内部工具：

- `artifact_read(artifact_id, offset=0, limit=4000)`，最多 8000 字符；小工具预算会自动缩小切片。
- `artifact_search(artifact_id, query, offset=0, limit=10)`，精确字面匹配，返回可回读的字符偏移。

外部 MCP 新增 `browser_artifact_read` / `browser_artifact_search`，额外要求已存在的
`run_id`。总计 14 个 MCP 工具。它们不接受任意路径，也不会访问另一个任务的目录。
外部调用方能看到任务标识和引用时，即可读取该任务材料；部署仍使用宿主已有的访问控制。
偏移针对保存的 JSON 文本，不是远端页面字节。读取工具无模型调用。

普通任务资料位于 `<task_state_root>/<run_id>/artifacts`，随原任务恢复可继续读取。
独立 ExtensionRuntime/ToolRegistry 使用自己的资料目录。此次未增加自动清理策略，
长时间运行时须由部署方管理保存期限；删除资料会使相应旧引用失效。

## 可恢复的压缩视图

每次规划前保存原始模型 payload。达到预算压力时：

1. 先移除重复的摘要索引、召回、旧来源和较旧的完整 action/result 记录。
2. 仍超限时，将大结果、正文或较旧记忆块替换成不可变资料引用。
   当前任务、计划、用户决定、Skill 指令和工具 Schema 保持不变；当前网页控件保留。
3. 只有新视图严格更小并实际落到目标预算内，才原子提交压缩记录并提供给模型。

事务记录保存在 `compactions/*.json`，包含 prepared/committed/aborted 状态、原视图引用、
新视图引用和压缩前后 token 估计。中断后 prepared 不会被误用为完成结果；重新计算，
或按输入、预算、系统提示的摘要匹配并校验后重用已提交视图。
原始视图、完整结果和源材料不被摘要覆盖。原有记忆摘要与相关性召回继续工作；
这一层新增的是确定性裁剪和证据引用，不额外引入无条件的 LLM 摘要调用。

服务明确报告 context-length 错误时，不再先删 response_format 重发同一请求。
planner 最多尝试一次明显更小、已提交的新视图；不能缩小时以
`status=incomplete, stopped_reason=context_budget` 停止。处理子智能体不裁掉输入，
超限记录保留失败回执，避免靠重复发送相同材料“修复”。

任务结果、MCP task/status/resume 现在返回 `context_budget`、当前 `context_view` 与最近已提交的 `last_compaction`。
`context_budget.json` 随处理输出保存；预算运行信息不进入方法内容哈希，因此服务探测
或 token 校准变化不会错误地使已验证处理记录失效。

## 设计参考及范围

参考之前审阅的官方 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness/tree/21638c56315ae6a2b552d6091945d3144c9af32e)
中 token-meter、spill 与 compaction 的职责分离。实现为本项目原有 Python 架构中的模块。

本轮没有把全部会话存储重写为事件溯源系统，也没有增加可持续对话的子智能体调度器，
没有自动修改 Skill 或模型训练。现有反思与经过宿主验证的经验库继续保留。
这些是后续独立阶段，不能从此次上下文测试推断出自我改善效果。

运行验证：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/live_context_eval.py --tag UNIQUE_CONTEXT_TAG
.\.venv\Scripts\python.exe scripts/live_harness_eval.py --tag UNIQUE_HARNESS_TAG
```

两类 live 脚本使用 30000 服务，输出到新目录，保存源文件快照、模型请求及验收回执。
上下文验收使用受控长材料和显式 14000 软限制，以便可靠触发压缩；不宣称完成 262K
满窗口压力测试。公开网页采集→子智能体加工的回归由另一个 live 脚本覆盖。

在真实回归中，本地模型曾把 `history_read` 写成顶层 action。现在只对宿主公开的工具名兼容该格式；注册表权限和参数 Schema 仍照常检查，未知工具不放行。自动选择的处理模型若在同一 job 中改变，立即中止，避免混入原方法哈希。

本轮记录见 [0.7 验收报告](CONTEXT_VALIDATION.md)。
