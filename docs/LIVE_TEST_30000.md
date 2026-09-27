# 30000 端口实测与底层重构审查

日期：2026-09-27。结论：需要重构浏览器执行契约与运行生命周期；保留已经走通的模型适配、Skill 和 MCP 扩展结构。本轮已经完成第一阶段修复，版本提升为 0.3.0。

## 测试设置与证据

- 实际接口：`http://127.0.0.1:30000/v1`，`/health`、`/v1/models` 和 `/props` 均实查。
- 实际模型 ID：`/data1/hgf_model/Qwen3.8-27B-Q5_K_M.gguf`，服务报告单 slot、262144 上下文。
- 同一套 6 个任务、同一模型参数：temperature=0、thinking=false、vision=false、maxTokens=1536，最多 12 步/任务、180 秒/任务，顺序运行。
- 5 个本地可验收网页与 1 个公开网页 `https://example.com/`。表单通过服务器收到的真实字段验收，iframe 通过真实点击触发的服务器状态验收，下载通过实际文件内容验收。Skill/MCP 检查真实调用日志与回答。
- 未替换模型输出。`scripts/live_eval.py` 仅在 HTTP 客户端记录实际请求、响应、耗时和服务返回的 usage。
- 本地证据：`logs/live-eval/baseline-30000-20260927/`、`logs/live-eval/refactor-30000-20260927/`。每轮包含源码快照、SHA-256、模型元数据；每个任务包含配置、请求响应、运行事件、结果和独立检查结果。旧输出保留，不覆写。

## 同一组任务的前后结果

| 任务 | 基线独立验收 | 重构后独立验收 | 模型调用数：前 → 后 | 关键证据 |
|---|---|---|---|---|
| 商品读取与条件筛选 | 通过 | 通过 | 1 → 1 | A17、129 元、库存 8 |
| 文本输入＋下拉框＋复选框＋提交 | 失败，stalled | 通过 | 9 → 6 | 服务器实际收到 Lin / lin@example.test / east / yes |
| 自然选择 Skill＋发现并调用外部 MCP | 通过 | 通过 | 5 → 5 | skill_load → tool_list → tool_describe → mcp.text.normalize_text；结果为 4 词 |
| 需要 Cookie 的 CSV 下载 | 通过，但直接下载 403 后绕回点击 | 通过，直接下载成功 | 3 → 2 | 文件 19 bytes，包含 A17,8 |
| iframe 内按钮操作 | 失败，但错误标记 completed | 通过 | 3 → 2 | frame_1:btn_1 被实际点击，主页面 Frame activated |
| 公开网站读取 | 通过 | 通过 | 1 → 1 | Example Domain 及实际来源 |

独立验收由 **4/6 → 6/6**；基线运行器曾报告 5/6 completed，说明不能用模型状态代替验收。
模型调用合计 **22 → 17**，服务报告 prompt tokens 合计 **106541 → 64953**，completion tokens **789 → 841**；六个任务耗时合计 **83.442 → 60.242 秒**。动作失败记录 **6 → 0**。
这些是单次小样本工程测试的观测值；任务路径和提示上下文同时改变，不能据此归因于某一项改动，也不能作为开放网站成功率或稳定性能提升的结论。

## 修复内容

1. **观察与动作对应。** 新增 `select_option(target_id,value OR label)`、`set_checked(target_id,checked)`，观察提供选项值、标签、禁用及选中状态。校验拒绝对 select 使用 type、未观察或禁用的选项。
2. **原生执行。** 移除大段 synthetic click/value/keyboard JavaScript，使用 Playwright click/fill/select_option/set_checked。目标绑定到当前观察的元素；元素被替换时失败并重新观察，不按旧序号误点新控件。真实浏览器回归测试断言点击事件 `isTrusted=true`。
3. **Frame 观察。** `browser/dom.py` 负责主文档和最多 8 个子 frame 的有界观察；frame 元素 ID 加前缀，执行回到对应 frame。原文档观察脚本移至 `browser/dom_observation.py`。
4. **浏览器会话下载。** 直接下载使用 `BrowserContext.request` 共享 Cookie，正确返回 HTTP 错误和空响应；点击产生的下载也进入 collected_files。文本保存统一 `.txt`，避免把正文伪装成 `.html`。
5. **明确结束语义。** `done` 必须提供 `outcome=completed|incomplete|blocked`；未启用 vision 时不可调用视觉动作。completed 仍明确标记 model_reported，尚未增加通用的独立 verifier。
6. **生命周期统一。** `harness/session.py` 在扩展启动前建立 run_id/日志/状态；runner 统一处理启动错误、截止时间和取消。只写一个终止事件，timeout 不再在日志中混写为 cancelled；外部取消仍传播给调用方。
7. **上下文精简。** 规划请求保留一份主要页面正文、控件与 frame 元数据，去掉同一页面多份文本及重复容器上下文；完整运行保留可追溯的观察/动作记录。

## 外部 MCP → CDP → 真实模型验收

另用 `scripts/live_mcp_cdp_eval.py` 启动独立 headless Chromium 和独立用户目录，由真实 stdio MCP 子进程连接其 CDP，再通过 30000 模型完成 iframe 任务。

验收：`browser_task` 返回 completed、真实服务器状态已激活、MCP 任务退出后附着的浏览器仍存活；之后测试脚本清理自己创建的浏览器。两步、8.037 秒。未连接用户日常浏览器或操作真实账户。

回执：`logs/live-eval/mcp-cdp-30000-20260927-r2/receipt.json`。首次测试驱动错误地把 errlog 传给 MCP Client，尚未发起任务即失败；修正到 stdio transport 后重跑，旧目录保留。此驱动修复不涉及 agent 生产逻辑。

以上实时测试的源码快照中版本字符串仍为 0.2.0，实测后提升为 0.3.0 并更新文档/Skill 的状态说明；版本标签本身不改变执行行为。具体运行代码以每轮的源码 SHA 和快照为准。

## 是否还需要更深的 Harness 重构

需要，但应分阶段；目前没有证据支持整体换成大型框架或增加多智能体。

| 优先级 | 仍需实现的能力 | 当前边界/下一步验收 |
|---|---|---|
| P1 | 宿主定义的完成检查器 | 模型可正确填写 outcome，也仍可能误判；下一步引入宿主侧 DOM/文件/工具检查及证据字段，失败时回到执行，不让网页或 Skill 自行声明验证成功 |
| P1 | 持久化 checkpoint 与恢复 | UUID JSONL 可以审计，不能恢复跨进程状态；需要保存任务、动作、工具状态及恢复游标，先验证副作用再继续，避免重复提交 |
| P1 | 浏览器事件和资源生命周期 | 目前点击/按键后有 700ms 观察窗口；延迟弹窗、延迟下载、页面关闭/跳转、对话框需要事件队列和明确的等待条件；下载响应仍一次性缓冲，不适合超大文件 |
| P2 | 更通用的页面适配 | 仅证明基本 iframe；未覆盖复杂跨域/深层嵌套 frame、大量控件、Shadow DOM、Canvas、自定义组件、上传、多选控件；当前选项最多 100、规划器最多 120 控件 |
| P2 | 精确上下文预算与记忆拆分 | 仍按字符估算 token，memory.py 较大；应分离任务状态、短期历史和长期记忆，增加 tokenizer 适配及大页面分页 |
| P2 | MCP/Skill 的长期生命周期 | 当前按 run 连接外部 MCP，Skill 只读指令及文本；长期连接、断线恢复、resources/prompts、脚本执行隔离需另立契约，不能从 Skill 内容自动获得主机执行权限 |

最值得继续的顺序是：**完成检查器 → 可恢复会话 → 浏览器事件处理 → 页面覆盖及上下文治理**。不要先投入更多提示词或叠加评审模型来掩盖执行层缺失。

## 复现

最终 0.3.0 回归：**35 passed in 13.84s**（`logs/validation-0.3/pytest.xml`），覆盖真实 Chromium 原生表单/可信点击、iframe、失效元素、Cookie 下载、点击下载登记，以及启动失败、超时日志一致性和取消清理。依赖检查与 Skill 静态校验通过。测试包含模型协议桩；与上面的真实模型验收分开计数。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
# 显式真实模型测试；tag 必须为新目录名，避免覆盖证据。
.\.venv\Scripts\python.exe -u scripts/live_eval.py --tag my-30000-run
.\.venv\Scripts\python.exe -u scripts/live_mcp_cdp_eval.py --tag my-mcp-cdp-run
```

日常调用可使用 `examples/harness-30000.json`；实际模型名留空以从服务发现。上述实时测试脚本不会进入普通 pytest/CI。

## 参考依据

- [Playwright 原生输入](https://playwright.dev/python/docs/input)：表单操作应使用明确的 fill/select_option/set_checked 等接口。当前实现绑定观察时的元素引用，失效后要求重新观察。
- [Playwright APIRequestContext](https://playwright.dev/python/docs/api/class-apirequestcontext)：context.request 与浏览器共享 Cookie。
- [Playwright frames](https://playwright.dev/python/docs/frames)：frame 是独立的页面交互上下文。
- [Anthropic：长任务 Harness](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)：跨轮次状态和真实验收的重要性。这里的分层和优先级是本项目审查后的工程判断，并非照搬其编码智能体方案。
- [Agent Skills 规范](https://agentskills.io/specification)：继续采用元数据、指令、资源渐进加载，独立于执行工具。
