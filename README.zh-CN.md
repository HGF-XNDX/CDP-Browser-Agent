# CDP Browser Agent

[English](README.md) | [中文](README.zh-CN.md)

从 UnifiedAgent 项目中拆分出来的独立 AI 浏览器智能体。

它使用 Playwright 控制浏览器，并通过任意 OpenAI-compatible
`/v1/chat/completions` 接口让模型规划浏览器动作。它既可以自己启动
Chromium，也可以通过 CDP 连接到已经打开的 Chrome/Edge 浏览器会话。

## 安装

直接从 GitHub 安装：

```powershell
pip install git+https://github.com/HGF-XNDX/CDP-Browser-Agent.git
python -m playwright install chromium
```

安装指定分支或 commit：

```powershell
pip install git+https://github.com/HGF-XNDX/CDP-Browser-Agent.git@main
```

本地开发安装：

```powershell
git clone https://github.com/HGF-XNDX/CDP-Browser-Agent.git
cd CDP-Browser-Agent
pip install -e .
python -m playwright install chromium
```

发布到 PyPI 之后，也可以使用：

```powershell
pip install cdp-browser-agent
python -m playwright install chromium
```

## 快速开始

连接本地 OpenAI-compatible 模型服务：

```powershell
browser-agent `
  --base-url http://127.0.0.1:8080/v1 `
  --model local-model `
  --headless `
  --max-steps 20 `
  "打开 https://example.com 并总结页面可见内容"
```

连接已有 CDP 浏览器：

```powershell
chrome.exe --remote-debugging-port=9222 --user-data-dir=D:\chrome-cdp-profile

browser-agent `
  --connection cdp `
  --cdp-url http://127.0.0.1:9222 `
  --base-url http://127.0.0.1:8080/v1 `
  "搜索 Playwright Python 文档并打开官方页面"
```

## Python API

```python
import asyncio

from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.runner import run_browser_agent


async def main():
    config = browser_agent_default_config()
    config["model"]["baseUrl"] = "http://127.0.0.1:8080/v1"
    config["browser"]["headless"] = True
    config["agent"]["max_steps"] = 20

    result = await run_browser_agent(
        "打开 https://example.com 并总结页面可见内容",
        config,
    )
    print(result)


asyncio.run(main())
```

## 配置

可以通过 `--config` 传入 JSON 配置覆盖文件。覆盖文件会和默认配置做深度合并。

```powershell
browser-agent --config examples/local-llamacpp.json "打开 https://example.com"
```

常用字段：

- `model.baseUrl`：OpenAI-compatible `/v1` 接口地址。
- `model.model`：模型名称。留空时会尝试从 `/props` 或 `/models` 自动发现。
- `model.enableThinking`：后端支持时启用 thinking。
- `browser.connection`：浏览器连接方式，可选 `launch` 或 `cdp`。
- `browser.downloads_path`：下载文件保存目录。
- `agent.max_steps`：浏览器规划动作的最大步数。
- `agent.strategy_evaluator_enabled`：启用第二次模型调用来评估候选动作。
- `agent.browser_site_memory_enabled`：启用跨运行的网站操作记忆。

## 说明

这个包保留了源项目中经过验证的浏览器可靠性能力，包括下载防护、无进展熔断、站点记忆，以及部分官方下载页面的确定性处理。这些规则属于浏览器智能体的安全和可靠性能力；具体业务领域的数据处理建议放在单独包或插件中。

## 发布

```powershell
python -m pip install build twine
python -m build
python -m twine check dist/*
```

检查通过后即可发布到你的包索引。
