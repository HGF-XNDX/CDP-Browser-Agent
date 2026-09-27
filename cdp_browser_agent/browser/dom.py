"""Frame-aware observations and native, actionability-checked browser inputs.

Targets bind to element handles from one observation. A rerender must fail closed
and be reobserved; it must never silently retarget another element by index.
"""
from __future__ import annotations

import asyncio

from playwright.async_api import Page

from .dom_observation import OBSERVE_SCRIPT


class BrowserDOM:
    def __init__(self, max_frames: int = 8, action_timeout_ms: int = 5000):
        self.max_frames = max_frames
        self.action_timeout_ms = action_timeout_ms
        self.targets = {}
        self.page = None

    def invalidate(self):
        self.targets.clear()
        self.page = None

    async def observe(self, page: Page) -> dict:
        self.invalidate()
        self.page = page
        observation = await page.main_frame.evaluate(OBSERVE_SCRIPT)
        for element in observation["elements"]:
            self.targets[element["id"]] = (page.main_frame, element["id"])
        observation["frames"] = []
        frames = [frame for frame in page.frames if frame != page.main_frame]
        observation["framesTruncated"] = len(frames) > self.max_frames
        for index, frame in enumerate(frames[:self.max_frames], 1):
            frame_id = f"frame_{index}"
            try:
                owner = await frame.frame_element()
                try:
                    if not await owner.is_visible():
                        continue
                finally:
                    await owner.dispose()
                child = await frame.evaluate(OBSERVE_SCRIPT)
                observation["frames"].append({"id": frame_id, "url": frame.url, "title": child.get("title", "")})
                for element in child["elements"]:
                    local_id = element["id"]
                    scoped_id = f"{frame_id}:{local_id}"
                    self.targets[scoped_id] = (frame, local_id)
                    observation["elements"].append({**element, "id": scoped_id, "frameId": frame_id})
                for key in ("fullText", "viewportText"):
                    observation[key] = (observation.get(key, "") + f"\n[Frame {frame_id}: {frame.url}]\n" + child.get(key, ""))[:40000]
            except Exception as exc:
                observation["frames"].append({"id": frame_id, "url": frame.url, "error": str(exc)[:300]})
        observation["fullTextLength"] = len(observation.get("fullText", ""))
        return observation

    async def execute(self, page: Page, action: dict) -> dict:
        name = action["action"]
        if name == "wait":
            await asyncio.sleep(action.get("ms", 1000) / 1000)
            return {"ok": True, "message": "waited"}
        if name == "press":
            await page.keyboard.press(action["key"])
            return {"ok": True, "message": "pressed " + action["key"]}
        if name == "scroll":
            return await page.evaluate("""amount => {
                const before = window.scrollY;
                window.scrollBy({top: amount, behavior: 'instant'});
                const after = window.scrollY;
                return {ok: before !== after, message: before !== after ? 'scrolled' : 'scroll_no_progress', before, after};
            }""", action["amount"])
        if name not in {"click", "type", "select_option", "set_checked"}:
            raise ValueError(f"Unsupported DOM action: {name}")
        binding = self.targets.get(action.get("target_id"))
        if self.page != page or not binding:
            raise ValueError("Stale or unobserved target; observe the current page again")
        frame, local_id = binding
        handle = await frame.evaluate_handle("id => window.__pwAgentElements?.[id] ?? null", local_id)
        try:
            element = handle.as_element()
            if element is None or not await element.evaluate("el => el.isConnected"):
                raise ValueError("Observed element is detached; observe again")
            if name == "click":
                await element.click(timeout=self.action_timeout_ms)
            elif name == "type":
                await element.fill(action["text"], timeout=self.action_timeout_ms)
            elif name == "select_option":
                key = "value" if "value" in action else "label"
                values = await element.select_option(**{key: action[key]}, timeout=self.action_timeout_ms)
                return {"ok": True, "message": "selected", "values": values}
            else:
                await element.set_checked(action["checked"], timeout=self.action_timeout_ms)
                return {"ok": True, "message": "checked_state_set", "checked": await element.is_checked()}
            return {"ok": True, "message": "clicked" if name == "click" else "typed"}
        finally:
            await handle.dispose()
