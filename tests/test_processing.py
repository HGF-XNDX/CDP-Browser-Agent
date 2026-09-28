import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.processing.engine import ProcessingEngine
from cdp_browser_agent.processing import engine
from cdp_browser_agent.workflows.runner import run_workflow
from cdp_browser_agent.browser.controller import BrowserController
from examples.workflows.demo_site import demo_server


@pytest.fixture
def processing(tmp_path):
    profile = {"name": "facts", "instructions": "Extract the title exactly. Do not invent missing facts.",
               "output_schema": {"type": "object", "properties": {"title": {"type": "string"}},
                                 "required": ["title"], "additionalProperties": False},
               "formats": ["json", "csv", "markdown"], "max_repairs": 1}
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    config = browser_agent_default_config()
    config["processing"] = {"paths": [str(path)], "artifact_dir": str(tmp_path / "processed")}
    config["model"]["model"] = "fixture"
    config["agent"]["log_dir"] = str(tmp_path / "logs")
    return config, profile, path


def test_processing_preview_is_bounded_without_silently_cutting_a_row():
    records = [{"record_key": str(i), "source_url": "https://example.com", "data": {"text": "x"*3000}} for i in range(3)]
    preview = engine.output_preview(records)
    assert len(json.dumps(preview, ensure_ascii=False)) <= 4000
    assert preview["truncated"] and preview["total_records"] == 3
    assert len(preview["records"]) == 1 and preview["records"][0]["data"] == records[0]["data"]


async def test_subagent_repairs_quotes_exports_and_reuses_success(processing, tmp_path, monkeypatch):
    config, _, _ = processing
    model = AsyncMock(side_effect=[json.dumps({"data": {"title": "Fact"}, "evidence": [{"field": "title", "quote": "invented"}]}),
                                  json.dumps({"data": {"title": "Fact"}, "evidence": [{"field": "title", "quote": "Fact"}]})])
    monkeypatch.setattr(engine, "chat_completion", model)
    records = [{"source_url": "https://example.com", "data": {"text": "Fact from a source"}}]
    result = await ProcessingEngine(config).run("facts", records, tmp_path / "out")
    assert result["ok"] and result["model_calls"] == 2
    assert {Path(p).suffix for p in result["artifact_paths"]} >= {".json", ".csv", ".md"}
    saved = json.loads(Path(result["records_path"]).read_text(encoding="utf-8"))
    assert saved[0]["source_url"] == "https://example.com" and saved[0]["data"]["title"] == "Fact"
    assert result["output_preview"]["records"][0]["data"] == saved[0]["data"]
    assert not result["output_preview"]["truncated"] and result["output_preview"]["total_records"] == 1
    again = await ProcessingEngine(config).run("facts", records, tmp_path / "out")
    assert again["reused_count"] == 1 and again["model_calls"] == 0 and model.await_count == 2
    prompts = model.call_args.args[0]
    assert "no browser" in prompts[0]["content"] and "output_schema" in prompts[1]["content"]


async def test_failed_records_not_exported_and_only_failures_retried(processing, tmp_path, monkeypatch):
    config, profile, path = processing
    profile["max_repairs"] = 0
    path.write_text(json.dumps(profile), encoding="utf-8")
    good = json.dumps({"data": {"title": "Fact"}, "evidence": [{"field": "title", "quote": "Fact"}]})
    model = AsyncMock(side_effect=[good, '{"data":{}}', good])
    monkeypatch.setattr(engine, "chat_completion", model)
    records = [{"source_url": f"https://example.com/{i}", "data": {"text": "Fact"}} for i in range(2)]
    result = await ProcessingEngine(config).run("facts", records, tmp_path / "out")
    assert result["status"] == "incomplete" and result["validated_count"] == 1
    assert len(json.loads(Path(result["records_path"]).read_text())) == 1
    assert result["output_preview"]["total_records"] == 1
    result = await ProcessingEngine(config).run("facts", records, tmp_path / "out")
    assert result["ok"] and result["reused_count"] == 1 and result["model_calls"] == 1
    profile["instructions"] += " Changed."
    path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        await ProcessingEngine(config).run("facts", records, tmp_path / "out")


async def test_mapping_no_model_and_csv_formula_escaped(processing, tmp_path, monkeypatch):
    config, profile, path = processing
    profile.update(mode="mapping", field_map={"title": "title"})
    path.write_text(json.dumps(profile), encoding="utf-8")
    model = AsyncMock(side_effect=AssertionError("mapping must not call a model"))
    monkeypatch.setattr(engine, "chat_completion", model)
    result = await ProcessingEngine(config).run("facts", [{"data": {"title": "=formula"}}], tmp_path / "out")
    assert result["ok"] and result["model_calls"] == 0
    assert "'=formula" in (tmp_path / "out/processed.csv").read_text(encoding="utf-8-sig")
    model.assert_not_awaited()


async def test_fetch_process_workflow_without_browser(processing, tmp_path, monkeypatch):
    config, profile, path = processing
    profile.update(mode="mapping", field_map={"title": "title"})
    path.write_text(json.dumps(profile), encoding="utf-8")
    browser = AsyncMock(side_effect=AssertionError("static pipeline must not start browser"))
    monkeypatch.setattr(BrowserController, "launch", browser)
    with demo_server() as (_, url):
        spec = {"schema_version": 1, "name": "pipeline", "version": "1", "description": "Read and process",
                "allowed_origins": [url], "min_records": 1,
                "steps": [{"id": "read", "type": "fetch", "urls": [url + "/catalog?page=1"], "browser_fallback": False},
                          {"id": "format", "type": "process", "input_step": "read", "profile": "facts"}]}
        file = tmp_path / "workflow.json"
        file.write_text(json.dumps(spec), encoding="utf-8")
        config["workflows"] = {"paths": [str(file)], "state_dir": str(tmp_path / "runs")}
        config["web"].update(proxy="", allowed_private_hosts=["127.0.0.1"], artifact_dir=str(tmp_path / "web"))
        result = await run_workflow("pipeline", config)
    assert result["status"] == "completed" and result["record_count"] == 1, result
    assert result["processing_results"]["format"]["validated_count"] == 1
    browser.assert_not_awaited()


async def test_partial_fetch_resume_retries_failed_sources_and_reuses_processing(processing, tmp_path):
    config, profile, path = processing
    profile.update(mode="mapping", field_map={"title": "title"})
    path.write_text(json.dumps(profile), encoding="utf-8")
    with demo_server() as (server, url):
        spec = {"schema_version": 1, "name": "partial", "version": "1", "description": "Retry failed sources",
                "allowed_origins": [url], "min_records": 2,
                "steps": [{"id": "read", "type": "fetch", "urls": [url + "/product/A17", url + "/product/B28"],
                           "on_error": "continue", "browser_fallback": False},
                          {"id": "format", "type": "process", "input_step": "read", "profile": "facts"}]}
        file = tmp_path / "workflow.json"
        file.write_text(json.dumps(spec), encoding="utf-8")
        config["workflows"] = {"paths": [str(file)], "state_dir": str(tmp_path / "runs")}
        config["web"].update(proxy="", allowed_private_hosts=["127.0.0.1"], artifact_dir=str(tmp_path / "web"))
        server.fail_paths.add("/product/B28")
        first = await run_workflow("partial", config)
        assert first["status"] == "incomplete" and first["record_count"] == 1
        count = server.requests["/product/A17"]
        server.fail_paths.clear()
        resumed = await run_workflow("partial", config, resume_run_id=first["run_id"])
        assert resumed["status"] == "completed" and resumed["record_count"] == 2
        assert server.requests["/product/A17"] == count
        assert resumed["processing_results"]["format"]["reused_count"] == 1


async def test_resumed_collector_hands_full_verified_source_to_worker(processing, tmp_path):
    import hashlib
    from cdp_browser_agent.harness.runtime import ExtensionRuntime
    config, profile, path = processing
    profile.update(mode="mapping", field_map={"title": "text"})
    path.write_text(json.dumps(profile), encoding="utf-8")
    config["web"]["artifact_dir"] = str(tmp_path / "web")
    source = tmp_path / "web/old-run/content.txt"
    source.parent.mkdir(parents=True)
    source.write_text("The full source, beyond the excerpt.", encoding="utf-8")
    async with ExtensionRuntime(config) as runtime:
        runtime.task_state = {"run_id": "a" * 32, "sources": [{"url": "https://example.com", "snippet": "The full",
            "kind": "web_fetch", "artifact_paths": [str(source)], "text_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}
        result = await runtime.registry.call("delegate_processing", {"profile": "facts"})
        assert result["ok"], result
        saved = json.loads(Path(result["records_path"]).read_text())
        assert saved[0]["data"]["title"] == source.read_text()
        source.write_text("changed", encoding="utf-8")
        result = await runtime.registry.call("delegate_processing", {"profile": "facts"})
        assert not result["ok"] and "hash" in result["message"]


async def test_processing_deadline_retains_completed_receipts(processing, tmp_path, monkeypatch):
    import asyncio
    config, profile, path = processing
    profile.update(timeout_seconds=1, max_repairs=0)
    path.write_text(json.dumps(profile), encoding="utf-8")
    calls = 0
    async def slow(*_):
        nonlocal calls
        calls += 1
        if calls == 2:
            await asyncio.sleep(10)
        return json.dumps({"data": {"title": "Fact"}, "evidence": [{"field": "title", "quote": "Fact"}]})
    monkeypatch.setattr(engine, "chat_completion", slow)
    records = [{"data": {"text": "Fact"}, "record_key": str(i)} for i in range(2)]
    with pytest.raises(asyncio.TimeoutError):
        await ProcessingEngine(config).run("facts", records, tmp_path / "out")
    result = await ProcessingEngine(config).run("facts", records, tmp_path / "out")
    assert result["ok"] and result["reused_count"] == 1


def test_cli_processing_utf8_and_model_override(processing, tmp_path):
    import subprocess
    import sys
    config, profile, path = processing
    profile.update(mode="mapping", field_map={"title": "title"})
    path.write_text(json.dumps(profile), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    records = tmp_path / "records.json"
    records.write_text(json.dumps([{"data": {"title": "中文标题"}}]), encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "cdp_browser_agent.browser", "--config", str(config_path),
        "--process-profile", "facts", "--processing-input", str(records), "--model", "cli-override"],
        capture_output=True, check=True, timeout=20)
    summary = json.loads(result.stdout.decode("utf-8"))
    assert summary["ok"] and summary["model_calls"] == 0
    frozen = json.loads(Path(summary["artifact_paths"][0]).read_text(encoding="utf-8"))
    assert frozen["model"]["model"] == "cli-override"
    rows = json.loads(Path(summary["records_path"]).read_text(encoding="utf-8"))
    assert rows[0]["data"]["title"] == "中文标题"


async def test_worker_rejects_context_overflow_before_calling_model(processing, tmp_path, monkeypatch):
    config, _, _ = processing
    config["model"].update(contextWindowTokens=4096, maxTokens=1536)
    model = AsyncMock(side_effect=AssertionError("Oversized input must not reach model"))
    monkeypatch.setattr(engine, "chat_completion", model)
    result = await ProcessingEngine(config).run("facts", [{"data": {"text": "中文正文" * 2000}}], tmp_path / "out")
    assert result["status"] == "incomplete" and result["model_calls"] == 0
    failures = json.loads((tmp_path / "out/failures.json").read_text(encoding="utf-8"))
    assert "context budget" in failures[0]["error"]


async def test_worker_does_not_mix_changed_auto_model_into_frozen_method(processing, tmp_path, monkeypatch):
    config, _, _ = processing
    async def discover(options):
        return {**options, "model": "first" if "_agent_context" not in options else "replacement"}
    monkeypatch.setattr(engine, "prepare_model_options", discover)
    model = AsyncMock()
    monkeypatch.setattr(engine, "chat_completion", model)
    with pytest.raises(ValueError, match="model changed"):
        await ProcessingEngine(config).run("facts", [{"data": {"text": "Fact"}}], tmp_path / "out")
    model.assert_not_awaited()
    frozen = json.loads((tmp_path / "out/method.json").read_text(encoding="utf-8"))
    assert frozen["model"]["model"] == "first"
