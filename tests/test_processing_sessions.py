import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp import Client

from cdp_browser_agent.processing import engine
from cdp_browser_agent.processing.engine import ProcessingEngine
from cdp_browser_agent.processing.sessions import ProcessingSessions, WorkerStore, WorkerBusy
from cdp_browser_agent.processing.learning import ProcedureStore, replay_experience, ReplayCatalog
from cdp_browser_agent.processing.verification import verify_delivery
from cdp_browser_agent.browser.default_config import browser_agent_default_config
from cdp_browser_agent.browser.runner import run_browser_agent
from cdp_browser_agent.browser import agent
from cdp_browser_agent.harness.verification import check_processing
from cdp_browser_agent.mcp_server import create_mcp_server


def record(label):
    return {"data": {"title": label, "preferred": "Release " + label, "text": label + "\nRelease " + label},
            "source_url": "https://example.com/" + label}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    profile = {"name": "headings", "instructions": "Use title unless feedback requests the release section heading.",
        "max_repairs": 0, "verification": [{"kind": "contained_in_input", "output": "title", "input": "text"}],
        "output_schema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"], "additionalProperties": False}}
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(json.dumps(profile), encoding="utf-8")
    suite = {"name": "heldout", "profile": "headings", "cases": [{"record": record(label), "expected": {"title": "Release " + label}} for label in ("Beta", "Gamma")]}
    suite_file = tmp_path / "suite.json"
    suite_file.write_text(json.dumps(suite), encoding="utf-8")
    config = browser_agent_default_config()
    config["processing"].update(paths=[str(profile_file)], replay_paths=[str(suite_file)], artifact_dir=str(tmp_path / "outputs"))
    config["agent"].update(log_dir=str(tmp_path / "logs"), max_steps=3)
    config["model"]["model"] = "fixture"
    async def answer(messages, options):
        payload = json.loads(messages[-1]["content"])
        context = payload["continuation"]
        key = "preferred" if context.get("feedback") or context.get("procedural_advice") else "title"
        title = payload["record"][key]
        return json.dumps({"data": {"title": title}, "evidence": [{"field": "title", "quote": title}]})
    calls = AsyncMock(side_effect=answer)
    monkeypatch.setattr(engine, "chat_completion", calls)
    return config, profile, profile_file, suite, suite_file, calls


async def conversation(config):
    service = ProcessingSessions(config)
    created = await service.create("headings", [record("Alpha")])
    first = await service.run(created["worker_session_id"])
    second = await ProcessingSessions(config).run(created["worker_session_id"], feedback="Use the release section heading", expected_turn=1)
    return first, second


async def test_continuation_keeps_old_outputs_frozen_inputs_and_event_cursor(setup):
    config, _, _, _, _, calls = setup
    first, second = await conversation(config)
    assert first["ok"] and second["ok"] and second["turn"] == 2
    assert first["records_path"] != second["records_path"]
    assert json.loads(Path(first["records_path"]).read_text())[0]["data"] == {"title": "Alpha"}
    assert json.loads(Path(second["records_path"]).read_text())[0]["data"] == {"title": "Release Alpha"}
    prompt = json.loads(calls.call_args.args[0][-1]["content"])
    assert prompt["continuation"]["previous"][0]["data"] == {"title": "Alpha"}
    assert prompt["record"] == record("Alpha")["data"]
    service = ProcessingSessions(config)
    status = service.status(second["worker_session_id"])
    assert any(e["kind"] == "message" for e in status["events"])
    assert service.status(second["worker_session_id"], after=status["event_cursor"])["events"] == []
    again = await service.run(second["worker_session_id"])
    assert again["turn"] == 2 and calls.await_count == 2
    with pytest.raises(ValueError, match="Stale"):
        await service.run(second["worker_session_id"], feedback="duplicate", expected_turn=1)
    assert calls.await_count == 2
    same = await service.run(second["worker_session_id"], feedback="Use the release section heading", expected_turn=2)
    assert same["turn"] == 2 and same["feedback_already_applied"] and calls.await_count == 2


async def test_turn_limit_parent_scope_and_changed_method_fail_closed(setup):
    config, profile, path, _, _, calls = setup
    config["processing"]["worker_max_turns"] = 1
    service = ProcessingSessions(config)
    child = await service.create("headings", [record("A")], parent_id="a" * 32)
    with pytest.raises(ValueError, match="parent"):
        await service.run(child["worker_session_id"], parent_id="b" * 32)
    profile["instructions"] += " Changed."
    path.write_text(json.dumps(profile))
    failed = await service.run(child["worker_session_id"], parent_id="a" * 32)
    assert failed["status"] == "failed" and "changed" in failed["error"]
    calls.assert_not_awaited()
    service.cancel(child["worker_session_id"])
    with pytest.raises(ValueError, match="turn budget"):
        await service.run(child["worker_session_id"], feedback="retry", expected_turn=1)


async def test_parent_interruption_resumes_partial_receipts_and_enforces_busy(setup):
    config, _, _, _, _, calls = setup
    original = calls.side_effect
    entered = asyncio.Event()
    async def slow(messages, options):
        if json.loads(messages[-1]["content"])["record"]["title"] == "B":
            entered.set()
            await asyncio.sleep(30)
        return await original(messages, options)
    calls.side_effect = slow
    service = ProcessingSessions(config)
    child = await service.create("headings", [record("A"), record("B")])
    job = asyncio.create_task(service.run(child["worker_session_id"]))
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(WorkerBusy):
        await service.run(child["worker_session_id"])
    another = await service.create("headings", [record("C")])
    with pytest.raises(WorkerBusy, match="concurrency"):
        await service.run(another["worker_session_id"])
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    state = service.status(child["worker_session_id"])
    assert state["status"] == "interrupted" and state["pending"] and not state["active"]
    calls.side_effect = original
    result = await ProcessingSessions(config).run(child["worker_session_id"])
    assert result["ok"] and result["reused_count"] == 1 and result["attempts"] == 2
    assert calls.await_count == 3


async def test_explicit_cancel_stops_inflight_model_and_requires_new_turn(setup):
    config, _, _, _, _, calls = setup
    entered = asyncio.Event()
    async def slow(*_):
        entered.set()
        await asyncio.sleep(30)
    calls.side_effect = slow
    service = ProcessingSessions(config)
    child = await service.create("headings", [record("A")])
    job = asyncio.create_task(service.run(child["worker_session_id"]))
    await asyncio.wait_for(entered.wait(), 5)
    ProcessingSessions(config).cancel(child["worker_session_id"])
    result = await asyncio.wait_for(job, 3)
    assert result["status"] == "cancelled" and not result["pending"]
    assert (await service.run(child["worker_session_id"]))["status"] == "cancelled"
    assert calls.await_count == 1


async def test_lease_expiry_recovers_pending_turn_and_attempt_budget_is_bounded(setup):
    config, _, _, _, _, _ = setup
    service = ProcessingSessions(config)
    child = await service.create("headings", [record("A")])
    identity = child["worker_session_id"]
    with WorkerStore(config) as store:
        state, _ = store.acquire(identity, None, None, "test")
        with store.db:
            store.db.execute("UPDATE workers SET lease=0 WHERE id=?", (identity,))
    assert (await service.run(identity))["ok"]
    assert service.status(identity)["attempts"] == 2


async def test_operator_contract_catches_supported_quote_but_wrong_value(setup, tmp_path):
    config, profile, path, _, _, calls = setup
    profile["verification"] = [{"kind": "equals_input", "output": "title", "input": "preferred"}]
    path.write_text(json.dumps(profile))
    result = await ProcessingEngine(config).run("headings", [record("A")], tmp_path / "bad")
    assert result["failed_count"] == 1 and not result["contract_verified"]
    failures = json.loads((tmp_path / "bad/failures.json").read_text())
    assert failures[0]["candidate"]["data"] == {"title": "A"}
    assert not failures[0]["verification"]["checks"][0]["ok"]
    assert json.loads((tmp_path / "bad/processed.json").read_text()) == []


async def test_delivery_hash_and_operator_checks_required_for_parent_completion(setup):
    config, _, _, _, _, _ = setup
    config["agent"]["completion_processing"] = [{"profile": "headings"}]
    service = ProcessingSessions(config)
    child = await service.create("headings", [record("A")], parent_id="a" * 32)
    result = await service.run(child["worker_session_id"])
    state = {"run_id": "a" * 32, "processing_results": [result]}
    assert check_processing(config, state)["ok"]
    config["agent"]["completion_processing"][0]["min_turn"] = 2
    assert not check_processing(config, state)["ok"]
    config["agent"]["completion_processing"][0]["min_turn"] = 1
    await service.run(child["worker_session_id"], feedback="release", expected_turn=1)
    assert not check_processing(config, state)["ok"]  # old revision is stale
    path = Path(result["records_path"]).parent / "processed.csv"
    path.write_text("tampered")
    assert not verify_delivery(result, config["processing"]["artifact_dir"])["ok"]


async def test_unfulfilled_processing_contract_rejects_model_done_without_browser(setup, monkeypatch):
    config, _, _, _, _, _ = setup
    config["agent"]["completion_processing"] = [{"profile": "headings"}]
    planner = AsyncMock(side_effect=[{"action": {"action": "done", "outcome": "completed", "answer": "done"}},
                                    {"action": {"action": "done", "outcome": "incomplete", "answer": "missing data"}}])
    monkeypatch.setattr(agent, "plan_next_action", planner)
    result = await run_browser_agent("Deliver checked data", config)
    assert result["status"] == "incomplete" and result["verification"]["ok"] is False
    assert not result["browser_started"]


async def test_paired_replay_promotes_then_new_jobs_adopt_and_revoke_stops_use(setup, tmp_path):
    config, _, _, _, _, _ = setup
    _, second = await conversation(config)
    identity = second["candidate_experience_id"]
    with ProcedureStore(config) as store:
        assert store.get(identity)["state"] == "candidate"
        assert not store.recall(second["method_hash"])
    replay = await replay_experience(config, identity, "heldout")
    assert replay["eligible"] and replay["promoted"] and len(replay["cases"]) == 2
    assert all(not c["baseline"]["passed"] and c["candidate"]["passed"] for c in replay["cases"])
    fresh = await ProcessingEngine(config).run("headings", [record("Fresh")], tmp_path / "fresh")
    assert fresh["experience_ids"] == [identity]
    assert json.loads(Path(fresh["records_path"]).read_text())[0]["data"]["title"] == "Release Fresh"
    with ProcedureStore(config) as store:
        store.revoke(identity)
    revoked = await ProcessingEngine(config).run("headings", [record("New")], tmp_path / "revoked")
    assert not revoked["experience_ids"]
    assert json.loads(Path(revoked["records_path"]).read_text())[0]["data"]["title"] == "New"


async def test_replay_overlap_and_method_change_rejected_before_inference(setup):
    config, profile, path, suite, suite_file, calls = setup
    _, second = await conversation(config)
    before = calls.await_count
    suite["cases"][0]["record"] = record("Alpha")
    suite_file.write_text(json.dumps(suite))
    with pytest.raises(ValueError, match="overlap"):
        await replay_experience(config, second["candidate_experience_id"], "heldout")
    assert calls.await_count == before
    suite["cases"][0]["record"] = record("Beta")
    suite_file.write_text(json.dumps(suite))
    profile["instructions"] += " changed"
    path.write_text(json.dumps(profile))
    with pytest.raises(ValueError, match="comparable"):
        await replay_experience(config, second["candidate_experience_id"], "heldout")
    assert calls.await_count == before


async def test_regression_or_equal_baseline_never_promotes(setup):
    config, _, _, suite, suite_file, calls = setup
    _, second = await conversation(config)
    identity = second["candidate_experience_id"]
    # Candidate fails an operator target on one case; no promotion.
    suite["cases"][0]["expected"] = {"title": "Beta"}
    suite_file.write_text(json.dumps(suite))
    report = await replay_experience(config, identity, "heldout")
    assert not report["eligible"] and not report["promoted"]
    # Both variants match all targets: also no evidence of improvement.
    async def equal(messages, options):
        data = json.loads(messages[-1]["content"])["record"]
        title = data["title"] if data["title"] == "Beta" else data["preferred"]
        return json.dumps({"data": {"title": title}, "evidence": [{"field": "title", "quote": title}]})
    calls.side_effect = equal
    report = await replay_experience(config, identity, "heldout")
    assert all(c["baseline"]["passed"] and c["candidate"]["passed"] for c in report["cases"])
    assert not report["promoted"]


async def test_mcp_worker_lifecycle_and_argument_boundaries(setup):
    config, _, _, _, _, _ = setup
    async with Client(create_mcp_server(config)) as client:
        started = await client.call_tool("browser_worker_start", {"profile": "headings", "records": [record("MCP")]})
        result = started.structured_content
        assert result["ok"] and result["turn"] == 1
        followed = await client.call_tool("browser_worker_continue", {"worker_session_id": result["worker_session_id"], "feedback": "release", "expected_turn": 1})
        assert followed.structured_content["turn"] == 2
        status = await client.call_tool("browser_worker_status", {"worker_session_id": result["worker_session_id"]})
        assert status.structured_content["events"]
        wrong = await client.call_tool("browser_worker_continue", {"worker_session_id": result["worker_session_id"], "feedback": "again", "expected_turn": 1})
        assert wrong.is_error
        invalid = await client.call_tool("browser_worker_status", {"worker_session_id": "../outside"})
        assert invalid.is_error


async def test_corrupt_input_and_attempt_budget_fail_before_inference(setup):
    config, _, _, _, _, calls = setup
    config["processing"]["worker_max_attempts"] = 1
    service = ProcessingSessions(config)
    child = await service.create("headings", [record("A")])
    identity = child["worker_session_id"]
    with WorkerStore(config) as store:
        saved = store.get(identity)
        path = store.root / identity / "artifacts" / (saved["input"]["artifact_id"] + ".json")
    path.write_text("{}")
    result = await service.run(identity)
    assert result["status"] == "failed" and "hash mismatch" in result["error"]
    with pytest.raises(ValueError, match="attempt budget"):
        await service.run(identity)
    calls.assert_not_awaited()


async def test_cancel_wins_commit_race_without_creating_advice(setup):
    config, _, _, _, _, _ = setup
    service = ProcessingSessions(config)
    child = await service.create("headings", [record("A")])
    identity = child["worker_session_id"]
    proposed = []
    with WorkerStore(config) as store:
        state, _ = store.acquire(identity, None, None, "caller")
        service.cancel(identity)
        state.update(status="completed", pending=False)
        store.finish(state, "turn_finished", before_commit=lambda: proposed.append(True))
        assert store.get(identity)["status"] == "cancelled"
    assert not proposed


async def test_replay_shares_admission_and_busy_does_not_demote_existing_advice(setup):
    config, _, _, _, _, calls = setup
    _, second = await conversation(config)
    identity = second["candidate_experience_id"]
    await replay_experience(config, identity, "heldout")
    service = ProcessingSessions(config)
    child = await service.create("headings", [record("Busy")])
    before = calls.await_count
    with WorkerStore(config) as store:
        state, _ = store.acquire(child["worker_session_id"], None, None, "test")
        with pytest.raises(WorkerBusy):
            await replay_experience(config, identity, "heldout")
        store.finish(state, "test_release")
        store.reserve_operation("replay:" + identity)
        with pytest.raises(WorkerBusy):
            await service.run(child["worker_session_id"])
        store.release_operation("replay:" + identity)
    with ProcedureStore(config) as learned:
        assert learned.get(identity)["state"] == "promoted"
    assert calls.await_count == before


async def test_auto_replay_failure_preserves_completed_worker(setup):
    config, _, _, _, suite_file, _ = setup
    config["processing"]["auto_replay"] = True
    suite_file.write_text("invalid json")
    _, second = await conversation(config)
    assert second["ok"] and second["status"] == "completed"
    assert second["experience_replay"]["promoted"] is False and second["experience_replay"]["error"]
    assert ProcessingSessions(config).status(second["worker_session_id"])["ok"]


async def test_successful_auto_replay_and_revocation_during_replay(setup):
    config, _, _, _, _, calls = setup
    config["processing"]["auto_replay"] = True
    _, second = await conversation(config)
    assert second["experience_replay"]["promoted"]
    original = calls.side_effect
    async def revoke_during_call(messages, options):
        with ProcedureStore(config) as store:
            store.revoke(second["candidate_experience_id"])
        return await original(messages, options)
    calls.side_effect = revoke_during_call
    with pytest.raises(ValueError, match="revoked"):
        await replay_experience(config, second["candidate_experience_id"], "heldout")
    with ProcedureStore(config) as store:
        assert store.get(second["candidate_experience_id"])["state"] == "revoked"
        assert not store.recall(second["method_hash"])


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "300"])
async def test_invalid_replay_timeout_rejected_before_inference(setup, timeout):
    config, _, _, _, _, calls = setup
    config["processing"]["replay_timeout_seconds"] = timeout
    with pytest.raises(ValueError, match="timeout"):
        await replay_experience(config, "unused", "heldout")
    calls.assert_not_awaited()
