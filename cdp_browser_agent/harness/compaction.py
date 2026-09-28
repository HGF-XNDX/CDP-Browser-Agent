"""Recoverable prompt projections. Evidence is immutable; pruning is a view.

The browser planner uses action/result records, not native tool-call messages.
Each record is pruned as a whole, so there can be no orphan tool result.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from ..context_budget import ContextBudgetExceeded
from ..workflows.store import atomic_json


class ContextCompactor:
    def __init__(self, artifacts, directory, recorder=None):
        self.artifacts = artifacts
        self.directory = Path(directory)
        self.recorder = recorder

    def prepare(self, payload, budget, system, *, image=None, target_ratio=1.0):
        def messages(value):
            content = json.dumps(value, ensure_ascii=False)
            if image:
                content = [{"type": "text", "text": content}, {"type": "image_url", "image_url": {"url": image, "detail": "high"}}]
            return [{"role": "system", "content": system}, {"role": "user", "content": content}]

        original = self.artifacts.save(payload)
        before = budget.estimate_messages(messages(payload))
        target = int(budget.available_prompt_tokens * target_ratio)
        if before <= target:
            return deepcopy(payload), {"status": "unchanged", "estimated_tokens": before, "target_tokens": target,
                                       "original": original}
        self.directory.mkdir(parents=True, exist_ok=True)
        signature = {"original": original["artifact_id"], "budget": budget.as_dict(), "target": target,
                     "system_sha256": hashlib.sha256(system.encode()).hexdigest(), "image": bool(image), "version": 1}
        identity = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        path = self.directory / (identity + ".json")
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
            if previous.get("status") == "committed":
                projected = self.artifacts.load(previous["projection"]["artifact_id"])
                if budget.estimate_messages(messages(projected)) <= target:
                    return projected, previous
        receipt = {"transaction_id": identity, "status": "prepared", "original": original,
                   "before_tokens": before, "target_tokens": target, "version": 1}
        atomic_json(path, receipt)
        view = deepcopy(payload)
        # Always leave an explicit way to recover the entire pre-prune view.
        view["context_archive"] = original
        removed = []

        def fits():
            return budget.estimate_messages(messages(view)) <= target

        try:
            # First prune redundant recall, retaining the newest action/result.
            for key in ("compressed_action_memory", "recalled_relevant_history", "site_memory", "verified_experience", "playbook_advice", "sources", "recent_history"):
                if fits():
                    break
                value = view.get(key)
                if not value:
                    continue
                if isinstance(value, list):
                    minimum = 1 if key == "recent_history" else 0
                    while len(value) > minimum and not fits():
                        value.pop(0)
                else:
                    view[key] = {}
                removed.append(key)
            # Then replace large evidence blocks with immutable references. User
            # task, plan, decisions, active skills and tool schemas are pinned.
            for key in ("last_result", "observation", "task_memory", "run_memory_brief", "recent_history"):
                if fits():
                    break
                value = view.get(key)
                if not value:
                    continue
                reference = self.artifacts.save(value)
                compact = {"artifact": reference, "compacted": True}
                if isinstance(value, dict):
                    if isinstance(value.get("artifact"), dict) and value["artifact"].get("artifact_id"):
                        compact["artifact"] = value["artifact"]
                    for field in ("ok", "status", "errorType", "url", "title", "needs_browser", "browser_url", "artifact_paths"):
                        if field in value:
                            compact[field] = value[field]
                    # Current controls and viewport state must remain actionable.
                    if key == "observation":
                        for field in ("elements", "viewport", "scroll", "pageType", "observationError", "frames"):
                            if field in value:
                                compact[field] = value[field]
                replacement = [compact] if isinstance(value, list) else compact
                if budget.estimate(replacement) < budget.estimate(value):
                    view[key] = replacement
                    removed.append(key)
            after = budget.estimate_messages(messages(view))
            if after >= before or after > target:
                raise ContextBudgetExceeded("Required task, plan, skill and tool context exceeds prompt budget; "
                                            "increase a supported soft limit or reduce tool/skill scope before resuming")
            projection = self.artifacts.save(view)
            receipt.update(status="committed", projection=projection, after_tokens=after, pruned_fields=removed)
            atomic_json(path, receipt)  # Commit before exposing the new view.
        except BaseException:
            receipt.update(status="aborted")
            atomic_json(path, receipt)
            raise
        if self.recorder:
            self.recorder.write("context_compaction", receipt)
        return view, receipt
