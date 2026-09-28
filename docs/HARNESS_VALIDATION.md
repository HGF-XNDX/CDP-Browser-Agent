# 0.6 验收记录

日期：2026-09-28。范围：独立处理子智能体、普通任务恢复、自主介入策略、上下文与经验管理，
以及采集/外部工具恢复修复。所有结果为有界工程检查，不能外推为开放网站成功率。

## 自动化检查

完整测试：**84 passed in 42.79s**。覆盖原有浏览器、真实 Chromium、stdio/HTTP MCP、
Skill 与工作流测试，以及本轮新增的关键路径：

- 方法与 Schema 控制处理；错误引文被拒绝并修复；失败项不进入有效导出。
- 输入/方法身份冻结，恢复复用成功记录，超时保留已完成回执。
- 无模型字段映射，CSV 公式转义；静态 fetch → process 不启动浏览器。
- CLI 输出 UTF-8 并正确应用模型覆盖参数；处理 worker 超出模型上下文预算时不发送请求。
- 部分采集失败恢复只重取失败网址，处理器复用已有成功项。
- 普通任务计划与记忆恢复；激活 Skill 恢复，正文改变后拒绝沿旧任务继续。
- 独占任务租约；抢占恢复失败不会污染正在运行的日志。
- auto 和等待超时后的自主继续；实际用户答复优先；过期响应被拒绝。
- 真实浏览器关闭后重启，cookies 与 localStorage 从快照恢复。
- 两次独立宿主通过才召回经验，按站点隔离，可撤销；中文预算与关键笔记保留。
- 时间戳不再伪装工具进展；小结果预算仍保留 URL、来源哈希与文件引用。
- 可选 MCP 服务失败不影响其他工具，required 服务失败仍阻断。
- 单个详情页失败后，同一列表内已取到的详情不重复抓取。

测试中的模型桩只证明控制流和机制。IndexedDB 使用 Playwright 保存接口，但该回归测试
直接断言的是 cookies/localStorage；不声称已验证所有站点登录状态。

## 30000 端口真实模型

执行脚本：`scripts/live_harness_eval.py --tag harness-30000-20260928-r1`。
端点：`http://127.0.0.1:30000/v1`；实际模型：`/data1/hgf_model/Qwen3.8-27B-Q5_K_M.gguf`。
本轮保持 maxTokens=1536、maxRetries=0，未替换模型或开启训练。

**9/9 检查通过：**

| 检查 | 实际证据 |
|---|---|
| live_fetch_to_real_processing | 真实读取 example.com，固定工作流交给真实模型加工，记录数 1 |
| schema_and_exact_quotes | JSON 匹配 Schema，summary 的原文引文在输入正文中 |
| three_export_formats | 实际生成 JSON、CSV、Markdown 文件 |
| real_model_plans_collects_delegates | 模型执行 plan_update → web_fetch → delegate_processing，之后更新计划并回查历史 |
| agent_has_validated_delivery | 普通任务保存通过校验的处理交付 |
| fast_task_no_browser | 普通采集与加工任务 browser_started=false |
| usage_reported | 任务报告模型调用次数、服务端 token 和耗时 |
| real_model_autonomous_continuation | 模型先询问语言，auto 策略记录 preauthorized，再抓取并完成中文回答 |
| real_stdio_processing | 独立 stdio MCP 进程调用 browser_process，真实模型产出有效记录 |

采集与加工普通任务 7 个步骤，模型成功响应 8 次（包含处理 worker），
服务端报告累计输入 40,583 tokens、输出 753 tokens、模型请求累计 25.812 秒。
这些是单次工程记录，不是成本优化前后对比或服务计费。整个同进程脚本记录 12 次模型响应，
另外 stdio 处理 worker 调用 1 次；MCP 子进程的网络报文没有算入同进程 model-wire。

真实模型未在本次测试中被强制等待 120 秒；等待超时和真实答复竞争由自动化测试覆盖。
也未执行长期自我改善的重复实验，不能声称经验已经提高任务成功率。

本地证据目录：`logs/live-harness/harness-30000-20260928-r1/`，含 `receipt.json`、
`metadata.json`、`source-snapshot.zip`、`model-wire.jsonl`、工作流来源与导出、普通任务日志、
MCP 处理结果。该目录由 Git 忽略，避免将运行数据、浏览器会话或模型报文提交到公开仓库。
脚本保存了当时运行的源代码哈希；其后补充的激活 Skill 恢复逻辑由完整 84 项测试验证。
公开仓库包含可复现脚本和本记录。

## 安装与分发

本地开发环境已升级到 0.6.0；`pip check` 无依赖冲突。
构建 wheel/sdist，校验发行包；内置 Skill 同步升级至 0.6 的接口，并使用 skill-creator
的 quick_validate 检查 frontmatter/命名。安装包包含处理模块、任务状态与经验模块、Skill
及其处理参考文档。Skill 验证不代替运行行为验收。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts/live_harness_eval.py --tag UNIQUE_TAG
.\.venv\Scripts\python.exe -m build --no-isolation
.\.venv\Scripts\python.exe -m twine check dist/cdp_browser_agent-0.6.0*
```

真实模型验收需有可用的本地服务和网络，只在显式运行脚本时执行，不进入 pytest/CI。
新功能配置与边界见 [处理与自主策略](PROCESSING_AUTONOMY.md)；架构研究见
[Harness 与上下文演进](HARNESS_EVOLUTION.md)。
