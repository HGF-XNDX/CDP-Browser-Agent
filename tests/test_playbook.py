import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp import Client

from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.harness.playbook import PlaybookStore, advice, scope, planner_method
from cdp_browser_agent.harness.learning import LearningService, ReplaySuites, run_evidence
from cdp_browser_agent.harness import learning
from cdp_browser_agent.workflows.spec import digest


@pytest.fixture
def config(tmp_path):
    value = browser_agent_default_config()
    value["agent"].update(log_dir=str(tmp_path / "logs"), max_steps=3)
    value["model"].update(model="fixture", modelDiscoveryTimeout=.1)
    value["processing"]["artifact_dir"] = str(tmp_path / "processed")
    value["learning"].update(enabled=True, auto_replay=False, state_dir=str(tmp_path / "learning"))
    return value


def evidence(config, method="method", target="planner", host="example.com", origin="original"):
    return {"scope": scope(config, target, method, host), "source": {"run_id": origin},
            "events": [{"id": "A1", "result": {"ok": False}}, {"id": "A2", "result": {"ok": True}}],
            "origin_hashes": [origin]}


def lesson(guidance="Inspect HTML before changing selectors"):
    return {"trigger": "Missing CSS fields", "guidance": guidance, "avoid": "Dynamic content needs a browser", "evidence_ids": ["A1", "A2"]}


def propose(store, config, **kwargs):
    source = store.save_evidence(evidence(config, **kwargs))
    return store.propose(lesson(), source)


def activate(store, entry):
    return store.record_replay(entry, {"status": "completed", "eligible": True, "candidate_all_passed": True})


def test_scope_budget_expiration_and_candidate_isolation(config):
    with PlaybookStore(config) as store:
        entry = propose(store, config)
        assert not store.recall(entry["scope"])
        activate(store, entry)
        assert store.recall(entry["scope"])[0]["id"] == entry["id"]
        for key, value in (("host", "other.example"), ("method_hash", "changed"), ("task_type", "other"), ("target", "processing")):
            assert not store.recall({**entry["scope"], key: value})
        store.settings["max_context_chars"] = 1
        assert not store.recall(entry["scope"])
        store.settings["max_context_chars"] = 6000
        with store.db:
            store.db.execute("UPDATE versions SET expires=0")
        assert not store.recall(entry["scope"])
        with pytest.raises(ValueError, match="expired"):
            store.validate_advice([advice(entry)])


def test_revision_keeps_active_until_replay_then_rolls_back(config):
    with PlaybookStore(config) as store:
        first = propose(store, config)
        activate(store, first)
        second = store.propose(lesson("Inspect HTML and test one item"), first["source_id"], identity=first["id"], expected_version=1)
        assert store.recall(first["scope"])[0]["version"] == 1
        with pytest.raises(ValueError, match="Stale"):
            store.propose(lesson("stale"), first["source_id"], identity=first["id"], expected_version=1)
        activate(store, second)
        assert store.recall(first["scope"])[0]["version"] == 2
        store.rollback(first["id"], 2, 1)
        assert store.recall(first["scope"])[0]["version"] == 1
        assert not activate(store, second)["promoted"]
        third = store.propose(lesson("new after rollback"), first["source_id"], identity=first["id"], expected_version=2)
        assert third["version"] == 3
        store.retire(first["id"], 3)
        assert not store.recall(first["scope"])
        assert not activate(store, third)["promoted"]
        assert {e["kind"] for e in store.history(first["id"])} >= {"proposed", "replay", "rollback", "retired"}


def test_failed_replay_demotes_active_and_retirement_wins_race(config):
    with PlaybookStore(config) as store:
        entry = propose(store, config)
        activate(store, entry)
        store.record_replay(entry, {"status": "completed", "eligible": False, "candidate_all_passed": False})
        assert not store.recall(entry["scope"])
        store.retire(entry["id"], 1)
        assert not activate(store, entry)["promoted"]


def test_exact_dedup_retains_all_origins_and_rejects_unknown_evidence(config):
    with PlaybookStore(config) as store:
        entry = propose(store, config)
        repeated = propose(store, config, origin="second")
        assert repeated["id"] == entry["id"] and len(store.list()["entries"]) == 1
        assert store.origin_hashes(entry["id"]) == {"original", "second"}
        with pytest.raises(ValueError, match="nonexistent"):
            store.propose({**lesson(), "evidence_ids": ["invented"]}, entry["source_id"])


def test_cross_process_job_lease_and_idempotency(config):
    with PlaybookStore(config) as first, PlaybookStore(config) as second:
        owner, _ = first.claim("same-source")
        assert second.claim("same-source") == (None, {"status": "busy"})
        first.finish_job("same-source", owner, {"status": "completed"})
        assert second.claim("same-source") == (None, {"status": "completed"})


def test_commit_rechecks_provenance_and_context_races(config):
    with PlaybookStore(config) as store:
        entry = propose(store, config)
        report = {"status": "completed", "eligible": True, "candidate_all_passed": True,
                  "cases": [{"input_sha256": "new-origin"}], "context_revision": store.context_revision(entry["scope"])}
        propose(store, config, origin="new-origin")
        assert not store.record_replay(entry, report)["promoted"]
        report["cases"] = []
        source = store.save_evidence(evidence(config, origin="elsewhere"))
        other = store.propose(lesson("Different lesson"), source)
        activate(store, other)
        assert not store.record_replay(entry, report)["promoted"]


def test_expired_duplicate_becomes_new_candidate_not_automatic_activation(config):
    with PlaybookStore(config) as store:
        entry = propose(store, config)
        activate(store, entry)
        with store.db:
            store.db.execute("UPDATE versions SET expires=0")
        refreshed = propose(store, config, origin="new-run")
        assert refreshed["version"] == 2 and refreshed["state"] == "candidate"
        assert not store.recall(entry["scope"])


async def test_reflector_curator_are_grounded_and_idempotent(config, monkeypatch):
    calls = AsyncMock(side_effect=[json.dumps({"lessons": [lesson()]}), json.dumps({"operations": [{"type": "ADD", "lesson": lesson()}]})])
    monkeypatch.setattr(learning, "chat_completion", calls)
    result = await LearningService(config).learn(evidence(config))
    assert result["status"] == "completed" and len(result["candidates"]) == 1
    again = await LearningService(config).learn(evidence(config))
    assert again == result and calls.await_count == 2
    with PlaybookStore(config) as store:
        assert store.list()["entries"][0]["state"] == "candidate"


@pytest.mark.parametrize("output", [
    {"lessons": [{**lesson(), "evidence_ids": ["invented"]}]},
    {"lessons": [{**lesson(), "permission": "allow all"}]},
])
async def test_invalid_reflection_cannot_change_store(config, monkeypatch, output):
    monkeypatch.setattr(learning, "chat_completion", AsyncMock(return_value=json.dumps(output)))
    result = await LearningService(config).learn(evidence(config))
    assert result["status"] == "failed"
    with PlaybookStore(config) as store:
        assert not store.list()["entries"]


async def test_curator_unsupported_operation_rejected(config, monkeypatch):
    calls = AsyncMock(side_effect=[json.dumps({"lessons": [lesson()]}), json.dumps({"operations": [{"type": "ACTIVATE", "lesson": lesson()}]})])
    monkeypatch.setattr(learning, "chat_completion", calls)
    result = await LearningService(config).learn(evidence(config))
    assert result["status"] == "failed"


def planner_suite(config, tmp_path):
    item = {"name": "selectors", "target": "planner", "task_type": "general", "host": "example.com",
            "cases": [{"task": "Inspect missing fields "+str(i), "observation": {"url": "https://example.com", "elements": []},
                       "expected": {"action": "tool", "name": "web_fetch", "arguments": {"content_format": "html"}}} for i in range(2)]}
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(item))
    config["learning"]["replay_paths"] = [str(path)]
    return item


async def test_real_planner_interface_paired_replay_and_gold_isolation(config, tmp_path, monkeypatch):
    from cdp_browser_agent.browser import planner
    from cdp_browser_agent.harness.runtime import ExtensionRuntime
    planner_suite(config, tmp_path)
    runtime = ExtensionRuntime(config)
    model = await learning.prepare_model_options(config["model"])
    with PlaybookStore(config) as store:
        entry = propose(store, config, method=planner_method(config, runtime, model))
    prompts = []
    async def generate(messages, options):
        payload = json.loads(messages[-1]["content"])
        prompts.append(payload)
        assert "expected" not in payload and "cases" not in payload
        fmt = "html" if payload["playbook_advice"] else "text"
        return json.dumps({"action": "tool", "name": "web_fetch", "arguments": {"url": "https://example.com", "content_format": fmt}})
    monkeypatch.setattr(planner, "chat_completion", generate)
    report = await LearningService(config).replay(entry["id"], 1, "selectors", runtime=runtime)
    assert report["promoted"] and len(prompts) == 4
    assert all(c["candidate"]["passed"] and not c["baseline"]["passed"] for c in report["cases"])
    with PlaybookStore(config) as store:
        assert store.recall(entry["scope"])


async def test_no_improvement_never_promotes(config, tmp_path, monkeypatch):
    from cdp_browser_agent.browser import planner
    from cdp_browser_agent.harness.runtime import ExtensionRuntime
    planner_suite(config, tmp_path)
    runtime = ExtensionRuntime(config)
    model = await learning.prepare_model_options(config["model"])
    with PlaybookStore(config) as store:
        entry = propose(store, config, method=planner_method(config, runtime, model))
    monkeypatch.setattr(planner, "chat_completion", AsyncMock(return_value=json.dumps({"action": "tool", "name": "web_fetch", "arguments": {"url": "https://example.com", "content_format": "html"}})))
    report = await LearningService(config).replay(entry["id"], 1, "selectors", runtime=runtime)
    assert report["candidate_all_passed"] and not report["promoted"]


async def test_overlap_and_method_drift_rejected(config, tmp_path):
    suite = planner_suite(config, tmp_path)
    with PlaybookStore(config) as store:
        entry = propose(store, config, origin=digest({"task": suite["cases"][0]["task"]}))
    with pytest.raises(ValueError, match="overlap"):
        await LearningService(config).replay(entry["id"], 1, "selectors")
    with PlaybookStore(config) as store:
        other_source = store.save_evidence(evidence(config, method="changed"))
        second = store.propose(lesson(), other_source)
    with pytest.raises(ValueError, match="changed"):
        await LearningService(config).replay(second["id"], 1, "selectors")


async def test_agent_calls_learning_on_failed_run_and_recall_on_next(config, monkeypatch):
    from cdp_browser_agent.browser import agent
    from cdp_browser_agent.browser.runner import run_browser_agent
    from cdp_browser_agent.harness.runtime import ExtensionRuntime
    runtime = ExtensionRuntime(config)
    model = await learning.prepare_model_options(config["model"])
    with PlaybookStore(config) as store:
        entry = propose(store, config, method=planner_method(config, runtime, model))
        activate(store, entry)
    task = "Read https://example.com"
    planner = AsyncMock(side_effect=[
        {"action": {"action": "tool", "name": "reflect", "arguments": {"summary": "fixture", "next_strategy": "inspect", "evidence_action_ids": ["missing"]}}},
        {"action": {"action": "done", "outcome": "incomplete", "answer": "unfinished"}}])
    monkeypatch.setattr(agent, "plan_next_action", planner)
    calls = AsyncMock(return_value=json.dumps({"lessons": []}))
    monkeypatch.setattr(learning, "chat_completion", calls)
    result = await run_browser_agent(task, config)
    assert result["playbook_learning"]["status"] == "no_lesson"
    assert planner.call_args_list[0].args[0]["memory_context"]["playbook_advice"][0]["id"] == entry["id"]
    assert calls.await_count == 1
    assert run_evidence(config, {**result, "status": "cancelled"}, "method") is None


async def test_processing_worker_autolearns_replays_and_fresh_turn_adopts(config, tmp_path, monkeypatch):
    from cdp_browser_agent.processing import engine
    from cdp_browser_agent.processing.sessions import ProcessingSessions
    profile = {"name": "heading", "instructions": "Extract the heading, using the releases preference when supplied.",
               "max_repairs": 0, "require_evidence": True, "verification": [{"kind": "contained_in_input", "output": "title", "input": "text"}],
               "output_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}}
    path = tmp_path / "method.json"
    path.write_text(json.dumps(profile))
    config["processing"]["paths"] = [str(path)]
    config["learning"]["auto_replay"] = True
    def record(name):
        return {"data": {"title": name, "text": name+"\nRelease "+name, "release": "Release "+name}}
    suite = {"name": "heading-check", "target": "processing", "task_type": "general", "host": "", "profile": "heading",
             "cases": [{"record": record(n), "expected": {"title": "Release "+n}} for n in ("Beta", "Gamma")]}
    replay_path = tmp_path / "replay.json"
    replay_path.write_text(json.dumps(suite))
    config["learning"]["replay_paths"] = [str(replay_path)]
    async def generate(messages, options):
        payload = json.loads(messages[-1]["content"])
        context = payload["continuation"]
        assert "expected" not in payload
        field = "release" if context.get("feedback") or context.get("playbook_advice") else "title"
        title = payload["record"][field]
        return json.dumps({"data": {"title": title}, "evidence": [{"field": "title", "quote": title}]})
    monkeypatch.setattr(engine, "chat_completion", generate)
    learned_lesson = {**lesson("Use the first release heading"), "evidence_ids": ["feedback", "current"]}
    monkeypatch.setattr(learning, "chat_completion", AsyncMock(side_effect=[json.dumps({"lessons": [learned_lesson]}),
        json.dumps({"operations": [{"type": "ADD", "lesson": learned_lesson}]})]))
    workers = ProcessingSessions(config)
    first = await workers.create("heading", [record("Alpha")])
    initial = await workers.run(first["worker_session_id"])
    corrected = await workers.run(first["worker_session_id"], feedback="Use releases", expected_turn=1)
    assert corrected["playbook_learning"]["replays"][0]["promoted"]
    repeated = await ProcessingSessions(config).run(first["worker_session_id"])
    assert repeated["playbook_learning"] == corrected["playbook_learning"]
    assert learning.chat_completion.await_count == 2
    fresh = await workers.create("heading", [record("Delta")])
    final = await workers.run(fresh["worker_session_id"])
    assert final["playbook_entries"] and json.loads(Path(final["records_path"]).read_text())[0]["data"]["title"] == "Release Delta"
    assert json.loads(Path(initial["records_path"]).read_text())[0]["data"]["title"] == "Alpha"


async def test_learning_defers_when_processing_slot_busy(config, monkeypatch):
    from cdp_browser_agent.processing.sessions import WorkerStore
    calls = AsyncMock(return_value=json.dumps({"lessons": []}))
    monkeypatch.setattr(learning, "chat_completion", calls)
    with WorkerStore(config) as workers:
        workers.reserve_operation("busy")
        try:
            result = await LearningService(config).learn(evidence(config))
            assert result["status"] == "deferred" and calls.await_count == 0
        finally:
            workers.release_operation("busy")
    resumed = await LearningService(config).learn(evidence(config))
    assert resumed["status"] == "no_lesson" and calls.await_count == 1


async def test_mcp_inspection_and_retirement(config):
    from cdp_browser_agent.mcp_server import create_mcp_server
    with PlaybookStore(config) as store:
        entry = propose(store, config)
        activate(store, entry)
    async with Client(create_mcp_server(config)) as client:
        result = await client.call_tool("browser_playbook_list", {})
        assert result.structured_content["entries"][0]["id"] == entry["id"]
        stale = await client.call_tool("browser_playbook_retire", {"entry_id": entry["id"], "expected_version": 2})
        assert stale.is_error
        result = await client.call_tool("browser_playbook_retire", {"entry_id": entry["id"], "expected_version": 1})
        assert result.structured_content["state"] == "retired"
