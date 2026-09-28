# 0.8 验收记录：2026-09-28

环境：Windows、项目 `.venv` Python 3.11、`http://127.0.0.1:30000/v1`，
实际模型 `/data1/hgf_model/Qwen3.8-27B-Q5_K_M.gguf`。测试没有替换成本地协议桩。
自动化单元测试使用协议桩的部分与下列真实模型验收分开统计。

## 验收结果

| 检查 | 结果 | 证据 |
|---|---|---|
| 完整自动化回归 | 125 passed，67.94 秒 | `python -m pytest -q` |
| 持久加工、反馈、经验回放及父任务完成核验 | 14/14 | `logs/live-workers/v080-workers-fixed-20260928/receipt.json` |
| 容量、压缩、原文找回及 MCP 回读 | 11/11 | `logs/live-context/v080-context-20260928/receipt.json` |
| 公开网页采集、加工、导出及自主继续 | 9/9 | `logs/live-harness/v080-harness-20260928/receipt.json` |
| 依赖检查 | 通过 | `python -m pip check` |
| Skill 结构验证 | 通过 | `dist/skills-0.8.0/cdp-browser-agent` |

三个 live 目录均保留源码快照；worker/context 回执和 harness metadata 中的全部
Python 源文件哈希与最终代码一致。worker 回执还记录了加工 profile 和回放集哈希。
原始模型请求与响应、导出、每轮输入、方法和验证回执均保存在各自目录。CLI/MCP
子进程的响应通过加工 attempt 文件保留，不计入父进程 model-wire 的请求数。

## 真实持续会话与改进结果

1. CLI 进程创建 worker、处理 Alpha 输入，然后退出。
2. 新 stdio MCP 进程读取相同会话，再发送一次反馈；会话从 turn 1 进入 turn 2。
3. 首轮输出 `Alpha Portal`，反馈后输出 `Alpha 2.0 Released`，此前输出文件哈希不变。
4. 方法和原始输入哈希一致；新建服务实例仍可读取两个完成事件。
5. 宿主对两个不同、未参与候选形成的输入执行原方法与候选方法的对照：

| 输入 | 原方法输出 | 候选输出 | 操作者目标 |
|---|---|---|---|
| Beta | Beta Portal | Beta 3.1 is available | Beta 3.1 is available |
| Gamma | Gamma Home | Gamma Spring Update | Gamma Spring Update |

候选两条通过，原方法两条未命中目标，经验获准晋升。新 worker 在 Delta 输入中
自动提取 `Delta 7 Released`；撤销经验后，新 worker 恢复提取 `Delta Portal`。
期望输出未写入加工模型提示。这个结果证明当前栏目偏好可以经过回放验证后复用，
不表示原方法本身在其默认任务上错误，也不表示泛化准确率提高。

另一个真实主任务 `web_fetch https://example.com → delegate_processing page-facts →
processing_continue` 保留父子关系，完成第二轮修订，输出 JSON/CSV/Markdown。
宿主重新读取交付文件、来源和规则后给出 `completion_basis=host_verified`，浏览器未启动。
该目录父进程记录 17 次真实模型请求，另有 CLI 首轮和 MCP 修订的真实请求。

## 首次实测发现的问题及保留证据

`logs/live-workers/v080-workers-20260928/receipt.json` 保留 **13/14** 的首次结果。
持续会话、回放晋升、复用和撤销均已通过；主智能体却不断提交同一句修订反馈，
直到用完 5 轮预算。交付文件实际存在，但不满足“只修订一次”的验收条件。

修复在宿主层对已经成功应用的相同反馈做幂等处理，并在响应中提供 applied_feedback
和 feedback_already_applied；同时补充最低轮次完成条件。最终实测中模型仍尝试重复
调用两次，但每次均返回第二轮已有结果，没有增加 worker 模型调用或输出版本。
这验证了执行层的节制措施，不意味着模型本身已消除重复规划倾向。

## 自动化测试覆盖的关键边界

新增 23 项测试覆盖跨实例继续、保留前轮、陈旧反馈和重复反馈、父任务隔离、方法变化、
中断后复用已验证记录、执行中取消、取消与结果提交竞争、租约到期、尝试预算、输入
篡改、交付文件篡改、最低修订轮次、配置断言失败、未完成却宣称 done、MCP 生命周期、
独立回放输入、模型/方法不可比、候选退化、相等表现、自动回放、并发占用、撤销竞争
及非法回放超时。125 项还包含既有真实 Chromium 和 stdio/HTTP MCP 集成测试。

没有在本轮再次实测长时间人工等待；继续沿用原有自动化覆盖和既有介入策略。
取消、进程租约过期和断点复用的故障注入使用自动化测试，未在真实模型推理进程上
进行破坏性故障演练。真实会话验证的是正常跨进程关闭、重开和继续。

## 上下文、兼容性与边界

服务容量仍自动识别为 262144 tokens；maxTokens=1536、ratio=0.85 时输入预算为
221516。长材料验收另设 14000 tokens 软限制，模型通过原文引用找回随机尾部标识，
压缩提交和重开回读均通过。没有测试满 262K 长距离语义准确率或吞吐性能。

旧的 `browser_process` 和 workflow `process` 回归通过。新增 7 个 MCP 工具，总数 21；
附带 Skill 和中英文 README 已更新。新加工校验只证明所配置条件及来源绑定，
`semantic_accuracy_verified` 仍为 false。所有测试都是小范围工程验收，不是任意网站、
真实业务文书或通用自主智能水平的基准。没有自动修改模型权重、程序或 Skill。

## 复现

下面三个脚本会调用已配置的本地模型。每次使用新 tag，旧目录不覆盖。

```powershell
python scripts/live_worker_eval.py --tag <new-worker-tag>
python scripts/live_context_eval.py --tag <new-context-tag>
python scripts/live_harness_eval.py --tag <new-harness-tag>
```

固定方法和配置在 [持续子会话指南](CONTINUING_WORKERS.md) 及
`examples/learning-30000.json`；回放期望由操作者维护。
