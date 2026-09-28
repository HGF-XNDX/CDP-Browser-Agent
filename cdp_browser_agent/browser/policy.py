"""Domain-independent action validation; the model cannot extend this boundary."""
from __future__ import annotations

from urllib.parse import urlparse
from jsonschema import validate


def _schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or []}


STRING = {"type": "string", "minLength": 1}
ACTION_SCHEMAS = {
    "click": _schema({"target_id": STRING}, ["target_id"]),
    "type": _schema({"target_id": STRING, "text": {"type": "string"}}, ["target_id", "text"]),
    "select_option": _schema({"target_id": STRING, "value": {"type": "string"}, "label": STRING}, ["target_id"]),
    "set_checked": _schema({"target_id": STRING, "checked": {"type": "boolean"}}, ["target_id", "checked"]),
    "press": _schema({"key": {"enum": ["Enter", "Tab", "Escape", "ArrowDown", "ArrowUp"]}}, ["key"]),
    "scroll": _schema({"amount": {"type": "integer", "minimum": -10000, "maximum": 10000}}, ["amount"]),
    "navigate": _schema({"url": STRING}, ["url"]),
    "open_tab": _schema({"url": STRING}, ["url"]),
    "switch_tab": _schema({"page_id": STRING}, ["page_id"]),
    "download": _schema({"url": STRING, "filename": STRING, "resource_name": STRING}, ["url"]),
    "save_page": _schema({"filename": STRING, "resource_name": STRING}),
    "back": _schema(),
    "wait": _schema({"ms": {"type": "integer", "minimum": 0, "maximum": 10000}}),
    "observe_vision": _schema(),
    "observe_browser": _schema(),
    "done": _schema({"answer": STRING, "outcome": {"enum": ["completed", "incomplete", "blocked"]}}, ["answer", "outcome"]),
    "ask_user": _schema({"message": STRING}, ["message"]),
    "tool": _schema({"name": STRING, "arguments": {"type": "object"}}, ["name", "arguments"]),
}
ALLOWED_ACTIONS = set(ACTION_SCHEMAS)


def validate_action(action: dict, observation: dict, request: dict | None = None) -> dict:
    # Some local models put an advertised tool name in the action field. Accept
    # that envelope only for host-advertised tools (plus legacy web aliases).
    # Registry authorization and argument schema validation still apply.
    advertised = {tool["name"] for tool in (request or {}).get("extensions", {}).get("builtin_tools", [])}
    aliases = advertised | {"web_search", "web_fetch"}
    if isinstance(action, dict) and action.get("action") in aliases - ALLOWED_ACTIONS:
        arguments = (action["arguments"] if set(action) == {"action", "arguments"}
                     else {key: value for key, value in action.items() if key != "action"})
        action = {"action": "tool", "name": action["action"], "arguments": arguments}
    if not isinstance(action, dict) or action.get("action") not in ACTION_SCHEMAS:
        raise ValueError("Unknown browser action")
    validate(action, ACTION_SCHEMAS[action["action"]])
    name = action["action"]
    if name in {"navigate", "open_tab", "download"}:
        parsed = urlparse(action["url"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Only http/https URLs without embedded credentials are allowed")
    if name == "observe_vision" and not (request or {}).get("model_settings", {}).get("enableVision", False):
        raise ValueError("Vision is disabled for this run; use DOM observations or report the limitation")
    if name in {"click", "type", "select_option", "set_checked"}:
        element = next((e for e in observation.get("elements", []) if e.get("id") == action["target_id"]), None)
        if element is None or element.get("disabled"):
            raise ValueError("Target must be a currently observed enabled element")
        if element.get("type") == "password" and not (request or {}).get("model_settings", {}).get("allowPasswordInput", False):
            return {"action": "ask_user", "message": "Password input requires user intervention."}
        if element.get("href"):
            action = {**action, "href": element["href"]}
        if name == "select_option":
            if element.get("tag") != "select" or ("value" in action) == ("label" in action):
                raise ValueError("select_option requires a select element and exactly one value or label")
            key = "value" if "value" in action else "label"
            if not any(o.get(key) == action[key] and not o.get("disabled") for o in element.get("options", [])):
                raise ValueError("Select an enabled option present in the current observation")
        if name == "type" and (element.get("tag") == "select" or element.get("type") in {"checkbox", "radio"}):
            raise ValueError("Use select_option or set_checked for this control, not type")
    if name == "switch_tab":
        pages = (request or {}).get("page_context", {}).get("pages", [])
        if not any(p.get("pageId") == action["page_id"] for p in pages):
            raise ValueError("Tab must be present in the current page context")
    return action
