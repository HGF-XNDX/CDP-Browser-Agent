# 本地验证记录

> 本文保留 0.2 阶段记录。0.3 的真实模型实测、底层改造及当前边界见 [30000 实测审查](LIVE_TEST_30000.md)。

日期：2026-09-27。环境：Windows、Python 3.11.15、项目内独立 `.venv`。

## 结果

- `python -m pytest -q --junitxml=logs/validation/pytest.xml`：**25 passed in 8.40s**。
- `python -m pip check`：No broken requirements found。
- `skill-creator/scripts/quick_validate.py`：内置 Skill 及导出副本均为 Skill is valid。
- `git diff --check`：通过；Git 仅提示本机 LF/CRLF 转换，无补丁空白错误。
- `python -m build --no-isolation`：生成 0.2.0 wheel 与 sdist。
- `python -m twine check`：wheel / sdist 均通过。

25 个测试包含真实 Chromium 执行、真实外部 stdio / Streamable HTTP MCP 通信、
对外 MCP 服务握手、Skill 读取与目录边界、工具参数和输出预算、运行超时和浏览器清理。
包括有外部 MCP 会话时取消整个运行的生命周期测试。

真实浏览器端到端使用本地 HTTP 模型响应桩，不使用付费模型：7 步完成 Skill 加载、
参考资料读取、外部加法工具调用、输入文本、Enter 提交、保存页面、报告完成。
断言了 DOM 中的 `Verified Harness`、外部工具结果 7、实际保存的文本内容。

## 关键依赖

| 包 | 本轮版本 |
|---|---|
| cdp-browser-agent | 0.2.0 |
| mcp | 2.2.0 |
| playwright | 1.63.0 |
| httpx | 0.28.1 |
| httpx2 | 2.13.1 |
| PyYAML | 6.0.3 |
| jsonschema | 4.26.0 |
| pytest | 9.1.1 |
| pytest-asyncio | 1.4.0 |

## 证据与边界

完整 JUnit 回执在本地 `logs/validation/pytest.xml`（日志目录不提交 Git）。
源码包保留测试和 fixture，wheel 保留可导出的 Skill。
CI 配置已添加 Windows/Linux 测试任务。本阶段记录仅报告 Windows 本地结果，GitHub CI 结果以对应提交的 Actions 运行状态为准。

没有执行真实模型的公开网站任务评测，没有验证不同模型的成功率、成本或性能，
没有进行用户登录态网站操作。浏览器 launch 路径有真实测试；用户已有 CDP 会话未做本轮 live 验收。
`completed` 仅代表内部规划器的报告，独立核实仍应查看对应页面、文件和事件证据。
