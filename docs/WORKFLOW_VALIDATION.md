# 0.4 工作流验收（2026-09-27）

## 自动检查

`.venv/Scripts/python.exe -m pytest -q`：46 passed，27.73 秒。
包括原有浏览器/Skill/MCP 回归，以及真实浏览器夹具的逐页暂停恢复、字段提取、详情页、
稳定键去重、增量比较、失败恢复、合作式暂停、取消持久化、并发租约、冻结定义、
宿主拒绝过早完成和原始快照哈希核验。

## 30000 真实模型

端点 `http://127.0.0.1:30000/v1`，实际模型
`/data1/hgf_model/Qwen3.8-27B-Q5_K_M.gguf`。
调用 `scripts/live_workflow_eval.py --tag workflows-30000-20260927`，结果保存在
`logs/live-workflows/workflows-30000-20260927/receipt.json`，9 项检查全部通过：

- 模型在本地演示网站打开目录，宿主核验实际列表。
- 第一页后暂停，保存 2 条记录；恢复后得到 3 条唯一记录及详情正文。
- 恢复过程没有新增模型调用，也没有重新访问已提交的第一页。
- 修改一个价格后新建运行，准确得到 1 changed、2 unchanged。
- 真实 stdio MCP 子进程完成相同工作流，并得到 3 unchanged。
- 公网 example.com 确定性采集通过。

该回执带源码快照、源码哈希、模型发现信息及直接模型请求/响应。
回执生成时源码仍标记 0.3，随后增加 HTML 快照保存、站点配置、文档并更新为 0.4；
最终回归的 46 项检查以及下述真实站点采集覆盖了这些后续实现。
夹具通过不代表文书网或任意复杂网站已支持。

## 实际知识产权正文采集

使用相同 30000 模型，通过项目自有 BrowserController/工作流访问真实官网。
`ipc-judgments-sample` 运行 ID：`979ae524b4974070af680f0b561ebe85`。
模型 2 步进入精品裁判列表；宿主核验 URL 和条目，然后确定性提取首屏 5 篇详情。
最终 `completed / host_verified`，`coverage: bounded`，不是采完整站。

输出：`workflow-runs/ip-collection/979ae524b4974070af680f0b561ebe85/`。
整理交付：`deliveries/ip-judgments-20260927/` 及同名 ZIP。
5 篇正文共 54,317 个字符；其中知识产权 4 篇、39,645 字符，竞争相关 1 篇单独保存。
导出脚本核对实际正文中的案号、判决书抬头、判决主文、终审标识及原始页面哈希。
网页发布日期与落款裁判日期分列，官方匿名处理保持原样。仅提取正文文本，未下载/OCR 图示。

## 中国裁判文书网

从首页正常检索“侵害发明专利权”，模型实际点击搜索后进入登录页，返回 `needs_input`，0 篇。
证据：`logs/site-discovery/wenshu-20260927/agent-ip-search/result.json`。
原始首页、列表和前端模板另保存在同目录；模板确认了 `.LM_list .caseName` 等结构，
但模板存在不等于会话中实际结果已经可读。

随后注册的 `wenshu-search` 工作流运行 ID：`90d8e848f6e54b1ca3edf0abb43eafd0`。
该次模型选择回车提交，只观察到页面框架，正确返回 `blocked / unverified / 0 records`。
两次阻塞形态分别保留，不能把没有加载的结果当成“没有匹配文书”。
该工作流只准备检索列表，不是已经验证的全文采集器。后续需要可用登录会话，
再核实筛选状态、JS 翻页、详情正文和可恢复定位；不得将普通 href 分页强套到该站。

官方入口：[中国裁判文书网](https://wenshu.court.gov.cn/)。
官方[使用帮助](https://wenshu.court.gov.cn/website/wenshu/181109AWZA70BFK4/index.html)
说明快捷/高级检索及登录后的查询模板功能；具体可用性以当前登录会话实测为准。
