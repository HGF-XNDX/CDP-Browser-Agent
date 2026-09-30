# Cockpit 本地 API 兼容与实测

日期：2026-09-30。模型保持用户指定的 `gpt-6-luna`，地址保持
`http://localhost:15536/v1`。密钥由 `CDP_BROWSER_AGENT_API_KEY` 环境变量解析。
此前读超时的原始运行、代码快照及复核包均保留，见
[首次恢复修复验收](DOCUMENT_RECOVERY_VALIDATION.md)。

## 已观察到的问题与修复

本机 Cockpit 使用 sidecar 网关。本地请求日志显示完整规划请求进入执行器，随后
持续等待，直到客户端期限到达才取消。同一服务的短 JSON 请求与相近长度的普通
参考文本请求成功，不能仅凭输入长度解释失败。

对原始规划输入作 Responses 流式诊断时，请求没有声明原生工具，但响应中的工具
列表包含 `image_generation`，并实际发出了三个 `image_generation_call`；两个调用
完成后仍没有文本规划答案，诊断在 75 秒终止。Cockpit 源码会添加该图片工具，并
提供请求级 `X-Agtools-Disable-Image-Generation: chat` 开关。依据分别为
[图片工具添加逻辑](https://github.com/jlcodes99/cockpit-tools/blob/main/sidecars/cockpit-cliproxy/third_party/CLIProxyAPI/internal/runtime/executor/codex_executor_request.go) 与
[请求头定义](https://github.com/jlcodes99/cockpit-tools/blob/main/sidecars/cockpit-cliproxy/third_party/CLIProxyAPI/internal/runtime/executor/helps/payload_helpers.go)。

只增加这个请求头、其余请求体与原始规划请求完全相同的一次诊断在 15.172 秒返回。
这支持采用请求级关闭方式。它不要求改 Cockpit 的全局配置、账号池、模型或端口。
运行源码没有 Cockpit 分支；通用 `model.extraHeaders` 由配置传给模型发现和生成
请求，格式兼容重试也保留它。显式解析的 API key 覆盖额外请求头中的旧认证值。
示例配置仅保存非密钥协议参数：

```json
{
  "model": {
    "provider": "openai",
    "baseUrl": "http://localhost:15536/v1",
    "apiEndpoint": "/responses",
    "model": "gpt-6-luna",
    "apiKeyEnv": "CDP_BROWSER_AGENT_API_KEY",
    "temperature": null,
    "extraHeaders": {"X-Agtools-Disable-Image-Generation": "chat"}
  }
}
```

关闭图片工具后，部分响应仍串联多个动作或模拟工具结果。普通非 JSON 模式及
Responses 的额外诊断没有解决这个问题。规划输入因此补充明确的单步协议：宿主在
响应结束后执行一次动作，下一次请求才提供实际结果。该修改的一次诊断在 4.922 秒
返回一个严格 JSON 动作；诊断输出从未用于构建任务候选。

第一次修复后的三国任务已取得并检查中国来源，但十次规划请求中出现空答案、损坏
JSON 和额外动作，任务因连续规划错误结束。新解析器使用 JSON 解码器读取第一个
完整动作，保留嵌套对象、转义引号和字符串中的花括号；后续内容不执行，选择回执及
完整原响应写入运行日志。它不修补损坏 JSON、不跳到后面的动作、不绕过动作权限、
候选复核或文件完整性门禁。这是所有来源共用的规划协议处理。

空 Chat 响应的一次原生 Responses 重放在 6.719 秒返回有效单步动作。由于重放涉及
新的生成，这不足以证明旧响应的正文一定被转换层丢失。客户端因此提供标准的
`model.apiEndpoint` 选项，保留默认 `/chat/completions`；本机 Luna 示例选择
`/responses`，直接解析 message/output_text，并统一其 input_tokens/output_tokens
使用量。宿主使用文本动作协议，原生工具选择设为 none；非完成终态即使 HTTP 200
也不会用于执行动作。`temperature: null` 表示省略此参数。输入保留文本指令、用户
文本/图片和助手文本，结构化输入输出转换参考
[OpenAI Responses 迁移说明](https://developers.openai.com/api/docs/guides/migrate-to-responses)。

评估日志还修复了 content=null 时对 None 切片的错误：空响应继续保留原始回执，
随后由模型客户端报告缺少文本，日志不会掩盖这个错误。所有最终运行使用修复后的
评估驱动，早期目录与回执保持原样。

原生接口运行进一步暴露了一个复核视图问题：默认标识可取整个来源单位，新增映射
同时携带完整 input、转换输出和输出标识，重复把长原文放进复核提示。真实 NASA
候选的提示估算为 63,412 token，超过 26,112 的本地提示预算，模型请求尚未发出就
被拒绝。映射展示现限制输入预览、转换值预览与抽样记录引用，保留原始字符数、
输出数量及完整映射定位。使用同一候选重新构造提示，估算为 14,104，候选哈希
不变；这里的 token 数是本地估算，实际 API 使用量另记在运行回执。复核协议更新
为 4，使原有审批需要按当前证据视图重新检查。完整候选和来源仍可恢复。

## 验证与范围

最终全量回归为 217 passed，89.42 秒；pip check、compileall 和 diff 检查通过。
新增协议检查覆盖模型发现与格式重试的请求头、原生 Responses 文本/图片转换、
使用量、HTTP 200 的失败终态、空响应日志、单步对象选择和长值/大范围映射视图。

`20260930-cockpit-final` 两文档任务已全部通过：PEP 索引 745 条、NASA RSS 10 条。
17 次实际模型请求包含 14 次规划和 3 次复核，均有使用量且没有接口失败。
运行源码在任务期间保持不变。独立来源断言核验每个单位各有一条记录、整段文本
归属、来源树保留以及记录哈希；最终文件门禁通过，智能体状态为 completed。
NASA 首个候选先被模型复核拒绝，规划器检查 title 节点后自行改变标识来源，再获
复核接受并导出；未向规划器提供这份规则或期望答案。

同一最终代码下的三国任务仍未完成，状态为 `max_steps`：60 次规划、7 次复核，
只有中国 82 条获接受并导出。中国输出的条文边界、各自正文、续段、来源树和
哈希均通过运行后独立断言。美国进行了六次候选修订，最后仍将废止条号 155 和
155A 合为一条；更换转换声明没有改变输出，有限诊断后该来源停止。日本取得并
解码了官方 XML，生成 464 条候选，但复核请求返回 HTTP 408，错误为上游流在
`response.completed` 前断开；随后多次读取候选片段没有产生审批结论，未导出。
日本候选没有经过导出后的来源断言，不能用其行数或来源树保留代替验收。

| 最终任务 | 已导出记录 | 来源独立验收 | 状态 |
| --- | ---: | --- | --- |
| PEP 索引 | 745 | 通过 | 完成 |
| NASA RSS | 10 | 通过 | 完成 |
| 中国专利法 | 82 | 通过 | 三国任务中的已完成来源 |
| 美国专利法 | 0 | 未执行输出验收 | 候选被拒绝，恢复停止 |
| 日本专利法 | 0 | 未执行输出验收 | 复核无结论，任务到达步数上限 |

三国运行中 66 个请求返回使用量，已知输入合计 882,564 token；日本失败复核
没有 usage，另计成本未知。历史回执的 `failed_requests` 仅统计传输异常，HTTP
错误另列在机器汇总，不能将这个计数为零解释为所有请求成功。当前同一 job 与
复核策略下的不确定结论会被缓存，读取片段不会重发复核，这是仍待解决的恢复
限制。最终代码已解决本次图片工具误入、接口转换及映射提示过大的问题，尚未
建立三国任务的完整自主处理能力。

后续冻结运行的结果及回归记录在
[机器验收汇总](validation/cockpit-text-20260930.json)。各次失败与成功分别保留，
运行结束后才使用来源专用断言检查输出，运行时没有提供国家选择器、条号规则或
预期答案。两组最终回归任务同时运行；耗时不作为单项性能或成本改善的因果证据。

接口恢复、单步解析、真实工具动作与完整任务交付是不同的验收层级。HTTP 200
以及 Cockpit 日志的 success 标志不能独立证明有完整答案；没有 usage 的取消请求
成本为未知，不能按零报告。相同模型在独立上下文中的复核也不能代替来源断言。
本轮不证明跨任务普遍可靠性、未知来源发现、最新法律版本或 PDF/OCR。

本机诊断位于 `logs/model-compatibility-20260930-luna`，实测位于
`logs/live-generic-documents/20260930-cockpit-text`、
`logs/live-generic-documents/20260930-cockpit-single-action` 和
`logs/live-document-transfer/20260930-cockpit-single-action`。
最终证据分别位于 `logs/live-document-transfer/20260930-cockpit-final` 与
`logs/live-generic-documents/20260930-cockpit-final`，导出位于对应 deliveries 目录。
Cockpit 原始账号资料、密钥及生成的图片数据不进入源码或复核包。
