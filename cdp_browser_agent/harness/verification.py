"""Host-owned page assertions used by tasks and workflow agent steps."""
from __future__ import annotations


async def check_page(controller, checks: list[dict]) -> dict:
    evidence = []
    for check in checks:
        try:
            kind = check["kind"]
            if kind == "url":
                actual = controller.page.url
                ok = actual.startswith(check["prefix"])
            else:
                locator = controller.page.locator(check["selector"])
                await locator.first.wait_for(state="visible", timeout=check.get("timeout_ms", 3000))
                actual = await locator.first.inner_text(timeout=3000) if kind == "text" else "visible"
                ok = check["contains"] in actual if kind == "text" else True
            evidence.append({"check": check, "ok": ok, "actual": actual[:2000]})
        except Exception as exc:
            evidence.append({"check": check, "ok": False, "error": str(exc)[:600]})
    return {"ok": bool(checks) and all(item["ok"] for item in evidence), "checks": evidence}
