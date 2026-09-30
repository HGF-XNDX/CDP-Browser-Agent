import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from cdp_browser_agent import model_client
from cdp_browser_agent.context_budget import ContextBudget, ContextBudgetExceeded, ContextWindowExceeded, route_key
from cdp_browser_agent.browser import planner
from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.memory import BrowserAgentMemory
from cdp_browser_agent.browser.policy import validate_action
from cdp_browser_agent.harness.artifacts import ArtifactStore
from cdp_browser_agent.harness.compaction import ContextCompactor
from cdp_browser_agent.harness.runtime import ExtensionRuntime
from cdp_browser_agent.harness.tools import Tool, ToolRegistry


def test_recent_exact_memory_keeps_tool_call_and_does_not_recommend_retry():
    from cdp_browser_agent.browser.memory import fallback_action_memory_fields, redact_action_payload
    memory = BrowserAgentMemory(agent_settings={}, model_settings={})
    entry = {'actionId': 'A0001', 'step': 1, 'url': 'about:blank',
        'action': {'action': 'tool', 'name': 'document_inspect', 'arguments': {'source_id': 'a' * 64,
            'selector': '.entry p', 'offset': 10, 'limit': 5, 'headers': {'authorization': 'secret-value'}}},
        'result': {'ok': True, 'total': 40, 'next_offset': 15}}
    fields = fallback_action_memory_fields(entry)
    record = memory._make_record(entry, fields)
    view = memory._format_exact_record(record)
    assert view['action_payload']['name'] == 'document_inspect'
    assert view['action_payload']['arguments']['selector'] == '.entry p'
    assert view['action_payload']['arguments']['offset'] == 10
    assert view['target'] == 'document_inspect'
    assert view['retry_recommendation'] == 'inspect_result'
    assert 'secret-value' not in json.dumps(view)
    assert entry['action']['arguments']['headers']['authorization'] == 'secret-value'
    action = {'action': 'tool', 'name': 'format', 'arguments': {'text': 'x' * 10000}}
    record['actionPayload'] = action
    assert memory._format_exact_record(record)['action_payload']['truncated']


def test_tool_result_memory_preserves_observed_samples_after_long_metadata():
    from cdp_browser_agent.browser.memory import fallback_action_memory_fields
    memory = BrowserAgentMemory(agent_settings={}, model_settings={})
    entry = {'actionId': 'A0092', 'step': 92, 'url': 'about:blank',
        'action': {'action': 'tool', 'name': 'catalog_read', 'arguments': {'offset': 10}},
        'result': {'ok': True, 'source': {'url': 'https://example.test/' + 'x' * 1200,
            'license': 'long source metadata ' * 100, 'headers': {'authorization': 'private-value'}},
            'total': 15, 'samples': [{'title': 'Rotor assembly', 'text': 'Disconnect power before servicing.'},
                {'title': 'Drive belt', 'text': 'Inspect tension after installation.'}], 'next_offset': None}}
    record = memory._make_record(entry, fallback_action_memory_fields(entry))
    formatted = memory._format_exact_record(record)
    assert 'Rotor assembly' in json.dumps(formatted) and 'Drive belt' in json.dumps(formatted)
    view = formatted['result_observation']
    encoded = json.dumps(view, ensure_ascii=False)
    assert 'Rotor assembly' in encoded and 'Drive belt' in encoded
    assert 'Disconnect power' in encoded and view['view']['total'] == 15
    assert view['truncated'] and len(encoded) <= 4200
    assert view['full_result']['arguments'] == {'action_id': 'A0092'}
    assert 'private-value' not in encoded
    assert entry['result']['source']['headers']['authorization'] == 'private-value'
    restored = BrowserAgentMemory(agent_settings={}, model_settings={})
    memory.raw_archive.append(record)
    restored.restore(memory.snapshot())
    assert restored._format_exact_record(restored.raw_archive[0])['result_observation'] == view
    legacy = memory.snapshot()
    legacy['raw_archive'][0].pop('resultObservation')
    restored.restore(legacy, history=[entry])
    assert restored._format_exact_record(restored.raw_archive[0])['result_observation'] == view


@pytest.fixture
def server(monkeypatch):
    model_client._capability_cache.clear()
    calls = []
    responses = {}
    def handle(request):
        calls.append(request)
        value = responses.get(request.url.path, (404, {}))
        if callable(value):
            return value(request)
        return httpx.Response(value[0], json=value[1])
    factory = httpx.AsyncClient
    monkeypatch.setattr(model_client.httpx, "AsyncClient", lambda **kw: factory(transport=httpx.MockTransport(handle), **kw))
    yield responses, calls
    model_client._capability_cache.clear()


def options(**extra):
    return {"baseUrl": "http://fixture/v1", "provider": "llama.cpp", "maxTokens": 1536, **extra}


async def test_discovers_actual_slot_capacity_and_caches(server):
    responses, calls = server
    responses["/props"] = (200, {"model_alias": "qwen", "default_generation_settings": {"n_ctx": 262144}, "total_slots": 1})
    model = await model_client.prepare_model_options(options())
    assert model["model"] == "qwen"
    budget = ContextBudget.from_settings(model)
    assert budget.model_capacity_tokens == budget.context_window_tokens == 262144
    assert budget.reserved_output_tokens == 1536
    await model_client.prepare_model_options(model)
    assert len(calls) == 1
    memory = BrowserAgentMemory(model_settings=model)
    assert memory._budget_for({}, {})["available_prompt_tokens"] == budget.available_prompt_tokens


async def test_models_fallback_matches_explicit_model_not_first_or_training(server):
    responses, calls = server
    responses["/v1/models"] = (200, {"data": [{"id": "other", "meta": {"n_ctx": 999999}},
        {"id": "chosen", "meta": {"n_ctx": 16384, "n_ctx_train": 1048576}}]})
    model = await model_client.prepare_model_options(options(provider="openai", model="chosen"))
    assert model["_model_capabilities"]["context_window_tokens"] == 16384
    assert [r.url.path for r in calls] == ["/v1/models"]
    assert all(r.url.path != "/props" for r in calls)


async def test_unknown_capacity_fallback_and_failures_are_not_permanent(server):
    responses, _ = server
    responses["/v1/models"] = (200, {"data": [{"id": "qwen", "meta": {"n_ctx_train": 999999}}]})
    result = await model_client.prepare_model_options(options())
    assert ContextBudget.from_settings(result).context_window_tokens == 32768
    assert ContextBudget.from_settings(result, {"context_window_tokens": 12000}).context_window_tokens == 12000
    model_client._capability_cache.clear()
    responses["/props"] = (200, {"model_alias": "qwen", "default_generation_settings": {"n_ctx": 262144}})
    assert (await model_client.prepare_model_options(options()))["_model_capabilities"]["context_window_tokens"] == 262144


async def test_auto_model_recovers_after_outage_and_server_reload(server):
    responses, _ = server
    offline = await model_client.prepare_model_options(options())
    assert offline["model"] == "local-model"
    model_client._capability_cache.clear()  # Expired outage entry.
    responses["/props"] = (200, {"model_alias": "real", "default_generation_settings": {"n_ctx": 262144}})
    recovered = await model_client.prepare_model_options(offline)
    assert recovered["model"] == "real" and recovered["_model_capabilities"]["context_window_tokens"] == 262144
    model_client._capability_cache.clear()
    responses["/props"] = (200, {"model_alias": "replacement", "default_generation_settings": {"n_ctx": 65536}})
    reloaded = await model_client.prepare_model_options(recovered)
    assert reloaded["model"] == "replacement"
    assert ContextBudget.from_settings(reloaded).context_window_tokens == 65536


async def test_props_for_another_model_is_not_a_capacity_match(server):
    responses, _ = server
    responses["/props"] = (200, {"model_alias": "other", "default_generation_settings": {"n_ctx": 262144}})
    responses["/v1/models"] = (200, {"data": [{"id": "other", "meta": {"n_ctx": 262144}}]})
    result = await model_client.prepare_model_options(options(model="explicit"))
    assert result["model"] == "explicit" and result["_model_capabilities"]["context_window_tokens"] is None


def test_explicit_limits_never_exceed_actual_capacity_or_get_overridden():
    model = options(contextWindowTokens=100000, _model_capabilities={"context_window_tokens": 262144, "source": "fixture"})
    budget = ContextBudget.from_settings(model, {"context_window_tokens": 32768, "reserved_output_tokens": 4096})
    assert budget.context_window_tokens == 32768 and budget.reserved_output_tokens == 4096
    model["_model_capabilities"]["context_window_tokens"] = 8192
    assert ContextBudget.from_settings(model).context_window_tokens == 8192
    with pytest.raises(ValueError):
        ContextBudget.from_settings(model, {"context_window_tokens": 512})


def test_usage_calibration_isolated_by_route_and_chinese_is_counted():
    first, second = options(model="one"), options(model="two")
    metrics = {"model_routes": {route_key(first): {"observed_chars_per_token": 1.1}}}
    assert ContextBudget.from_settings(first, metrics=metrics).chars_per_token == 1.1
    assert ContextBudget.from_settings(second, metrics=metrics).chars_per_token == 3
    budget = ContextBudget.from_settings(first)
    assert budget.estimate("中文" * 100) > budget.estimate("ab" * 100)
    assert route_key(first) != route_key({**first, "apiKey": "another-account"})


async def test_provider_overflow_does_not_retry_identical_payload_or_strip_format(server):
    responses, calls = server
    responses["/v1/chat/completions"] = (400, {"error": {"code": "context_length_exceeded", "message": "too long"}})
    with pytest.raises(ContextWindowExceeded):
        await model_client.chat_completion([{"role": "user", "content": "small"}], options(model="fixture"))
    assert len([r for r in calls if r.method == "POST"]) == 1


async def test_format_compatibility_retry_still_works(server):
    responses, calls = server
    def respond(request):
        if "response_format" in json.loads(request.content):
            return httpx.Response(400, json={"error": {"message": "response_format unsupported"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    responses["/v1/chat/completions"] = respond
    assert await model_client.chat_completion([{"role": "user", "content": "small"}], options(model="fixture")) == '{"ok":true}'
    assert len([r for r in calls if r.method == "POST"]) == 2


async def test_generic_tool_result_retained_readable_after_restart(tmp_path):
    original = {"ok": True, "records": [{"text": "前文" * 9000 + "末尾证据-917", "source": "https://example.com"}]}
    store = ArtifactStore(tmp_path)
    registry = ToolRegistry(max_result_chars=512, artifact_store=store)
    call = AsyncMock(return_value=original)
    registry.register(Tool("fixture", "large", {"type": "object"}, call))
    result = await registry.call("fixture", {})
    identity = result["artifact"]["artifact_id"]
    reopened = ArtifactStore(tmp_path)
    assert reopened.load(identity) == original and call.await_count == 1
    hit = reopened.search(identity, "末尾证据-917")["matches"][0]
    assert reopened.read(identity, hit["offset"], 100)["text"].startswith("末尾证据-917")
    with pytest.raises(ValueError):
        reopened.read("../outside")
    with pytest.raises(FileNotFoundError):
        ArtifactStore(tmp_path / "other-run").read(identity)
    (tmp_path / f"{identity}.json").write_text("tampered")
    with pytest.raises(ValueError, match="hash"):
        reopened.read(identity)


async def test_artifact_tools_return_actual_slices_under_small_budget(tmp_path):
    config = browser_agent_default_config()
    config["harness"].update(max_tool_result_chars=512, artifact_dir=str(tmp_path))
    runtime = ExtensionRuntime(config)
    ref = runtime.registry.artifacts.save({"text": "data" * 1000 + "尾部"})
    first = await runtime.registry.call("artifact_read", {"artifact_id": ref["artifact_id"], "limit": 8000})
    assert first["ok"] and first["text"] and not first.get("truncated")
    assert first["next_offset"] > first["offset"]
    search = await runtime.registry.call("artifact_search", {"artifact_id": ref["artifact_id"], "query": "尾部"})
    assert search["matches"] and not search.get("truncated")


async def test_only_advertised_tool_aliases_normalized_and_schema_still_checked(tmp_path):
    config = browser_agent_default_config()
    config["agent"]["log_dir"] = str(tmp_path)
    runtime = ExtensionRuntime(config)
    runtime.task_state = {"history": [{"actionId": "A0005", "result": {"ok": True, "message": "saved"}}]}
    request = {"extensions": runtime.context()}
    action = validate_action({"action": "history_read", "action_id": "A0005"}, {}, request)
    assert action == {"action": "tool", "name": "history_read", "arguments": {"action_id": "A0005"}}
    assert (await runtime.registry.call(action["name"], action["arguments"]))["data"]["actionId"] == "A0005"
    bad = validate_action({"action": "history_read", "action_id": 5}, {}, request)
    assert not (await runtime.registry.call(bad["name"], bad["arguments"]))["ok"]
    with pytest.raises(ValueError, match="Unknown"):
        validate_action({"action": "unregistered_shell", "command": "anything"}, {}, request)
    with pytest.raises(ValueError, match="Unknown"):
        validate_action({"action": "history_read", "action_id": "A0005"}, {}, {})


def test_compaction_commits_replays_and_preserves_pinned_state_and_original(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    compact = ContextCompactor(store, tmp_path / "compactions")
    budget = ContextBudget.from_settings(options(contextWindowTokens=8192))
    payload = {"task": "Read evidence", "run_notes": {"plan": ["Keep"], "decisions": ["User reply"]},
               "extensions": {"active_skills": ["Exact instructions"]},
               "recent_history": [{"action": "read", "result": "中文" * 2000} for _ in range(8)],
               "last_result": {"ok": True, "text": "证据" * 8000}}
    view, receipt = compact.prepare(payload, budget, "system")
    assert receipt["status"] == "committed" and receipt["after_tokens"] < receipt["before_tokens"]
    for key in ("task", "run_notes", "extensions"):
        assert view[key] == payload[key]
    assert store.load(receipt["original"]["artifact_id"]) == payload
    replay, again = ContextCompactor(store, compact.directory).prepare(payload, budget, "system")
    assert replay == view and again == receipt
    # Simulate interrupted pre-commit state. Original evidence remains usable.
    path = compact.directory / (receipt["transaction_id"] + ".json")
    path.write_text(json.dumps({**receipt, "status": "prepared", "projection": None}))
    recovered, recovered_receipt = compact.prepare(payload, budget, "system")
    assert recovered == view and recovered_receipt["status"] == "committed"


def test_unshrinkable_context_aborts_without_losing_evidence(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    compact = ContextCompactor(store, tmp_path / "compactions")
    payload = {"task": "中文任务" * 5000}
    with pytest.raises(ContextBudgetExceeded):
        compact.prepare(payload, ContextBudget.from_settings(options(contextWindowTokens=4096)), "system")
    receipt = json.loads(next(compact.directory.glob("*.json")).read_text())
    assert receipt["status"] == "aborted" and "projection" not in receipt
    assert store.load(receipt["original"]["artifact_id"]) == payload


async def test_planner_overflow_recovery_requires_smaller_committed_view(tmp_path, monkeypatch):
    model = options(model="fixture", _model_capabilities={"context_window_tokens": 262144, "source": "fixture"})
    monkeypatch.setattr(planner, "prepare_model_options", AsyncMock(return_value=model))
    call = AsyncMock(side_effect=[ContextWindowExceeded("too long"), '{"action":"done","outcome":"incomplete","answer":"partial"}'])
    monkeypatch.setattr(planner, "chat_completion", call)
    compactor = ContextCompactor(ArtifactStore(tmp_path / "artifacts"), tmp_path / "compactions")
    request = {"task": "Read", "step": 1, "model_settings": model, "last_result": {"ok": True, "text": "long evidence " * 18000}, "compactor": compactor}
    result = await planner.plan_next_action(request)
    assert result["compaction"]["status"] == "committed" and call.await_count == 2
    assert len(json.dumps(call.call_args_list[1].args[0])) < len(json.dumps(call.call_args_list[0].args[0]))


async def test_planner_does_not_retry_when_required_context_cannot_shrink(tmp_path, monkeypatch):
    model = options(model="fixture")
    monkeypatch.setattr(planner, "prepare_model_options", AsyncMock(return_value=model))
    call = AsyncMock(side_effect=ContextWindowExceeded("too long"))
    monkeypatch.setattr(planner, "chat_completion", call)
    request = {"task": "Read", "step": 1, "model_settings": model,
               "compactor": ContextCompactor(ArtifactStore(tmp_path / "artifacts"), tmp_path / "compactions")}
    with pytest.raises(ContextBudgetExceeded):
        await planner.plan_next_action(request)
    assert call.await_count == 1


@pytest.mark.parametrize('structured', [True, False])
async def test_planner_respects_empty_structured_history_and_keeps_legacy_fallback(tmp_path, monkeypatch, structured):
    model = options(model='fixture')
    monkeypatch.setattr(planner, 'prepare_model_options', AsyncMock(return_value=model))
    call = AsyncMock(return_value='{"action":"done","outcome":"incomplete","answer":"partial"}')
    monkeypatch.setattr(planner, 'chat_completion', call)
    memory = [{'actionId': f'A{i:04}', 'step': i, 'action': 'read',
               'result': f'legacy result {i}'} for i in range(1, 13)]
    request = {'task': 'Read a catalog', 'step': 13, 'model_settings': model, 'memory': memory,
        'compactor': ContextCompactor(ArtifactStore(tmp_path / 'artifacts'), tmp_path / 'compactions')}
    if structured:
        request['memory_context'] = {'recent_exact_history': [], 'compressed_action_memory': []}
    await planner.plan_next_action(request)
    payload = json.loads(call.call_args.args[0][1]['content'])
    if structured:
        assert payload['recent_history'] == []
        assert payload['compressed_action_memory'] == []
    else:
        assert len(payload['recent_history']) == 10
        assert len(payload['compressed_action_memory']) == 2
