from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from playwright.async_api import Browser, Page, async_playwright


DATA_DIR_NAME = "data"
ANALYSIS_DIR_NAME = "analysis"

SAVE_PAGE_SCRIPT = r"""() => ({
    title: document.title || "",
    url: location.href,
    text: String(document.body?.innerText || document.documentElement?.innerText || "").replace(/\r/g, "").trim()
})"""


def safe_resource_name(value: str, fallback: str = "downloaded_resource") -> str:
    text = unquote(str(value or "")).strip().lower()
    text = re.sub(r"[\s.-]+", "_", text)
    text = "".join(char for char in text if char.isalnum() or char == "_")
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:100] or fallback


def infer_resource_name(url: str, filename: str | None = None) -> str:
    parsed = urlparse(url or "")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    hostname = (parsed.hostname or "").lower()
    if hostname == "huggingface.co" and len(parts) >= 3 and parts[0] == "datasets":
        return safe_resource_name(parts[2])
    if hostname.endswith("github.com") and len(parts) >= 2:
        return safe_resource_name(parts[1])
    candidate = Path(filename or (parts[-1] if parts else "")).stem
    generic_names = {"train", "test", "validation", "data", "dataset", "downloaded_resource", "link"}
    if candidate and safe_resource_name(candidate) not in generic_names:
        return safe_resource_name(candidate)
    return safe_resource_name((hostname.split(".")[0] if hostname else "") or "downloaded_resource")


OBSERVE_SCRIPT = r"""
() => {
  window.__pwAgentElements = {};

  function normalizeText(text, limit = 500) {
    return String(text || "").replace(/\s+/g, " ").trim().slice(0, limit);
  }

  function isVisible(el) {
    if (!(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function inViewport(rect) {
    return rect.bottom >= 0 && rect.right >= 0 && rect.top <= window.innerHeight && rect.left <= window.innerWidth;
  }

  function textOf(el) {
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return "";
    return normalizeText(el.getAttribute("aria-label") || el.getAttribute("title") || el.innerText || el.textContent || "", 300);
  }

  function nearbyText(el, limit = 500) {
    const chunks = [];
    const label = associatedLabel(el);
    if (label) chunks.push(label);
    for (const attr of ["aria-label", "title", "placeholder", "name"]) {
      const value = normalizeText(el.getAttribute(attr) || "", 120);
      if (value) chunks.push(value);
    }
    const container = el.closest("label, form, [role='form'], .login, .form, .checkbox, .radio") || el.parentElement;
    if (container) {
      const text = normalizeText(container.innerText || container.textContent || "", limit);
      if (text) chunks.push(text);
    }
    return Array.from(new Set(chunks)).join(" | ").slice(0, limit);
  }

  function getViewportText(limit = 10000) {
    const root = document.body || document.documentElement;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const text = normalizeText(node.nodeValue, 200);
        if (!text) return NodeFilter.FILTER_REJECT;
        const parent = node.parentElement;
        if (!parent || !isVisible(parent)) return NodeFilter.FILTER_REJECT;
        if (!inViewport(parent.getBoundingClientRect())) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    const chunks = [];
    let total = 0;
    while (walker.nextNode() && total < limit) {
      const text = normalizeText(walker.currentNode.nodeValue, 400);
      if (text) {
        chunks.push(text);
        total += text.length + 1;
      }
    }
    return chunks.join("\n").slice(0, limit);
  }

  function fullDocText(limit = 40000) {
    // Entire document body text, NOT limited to the current viewport. Lets the
    // planner read a long static document (e.g. a law article page) in one shot
    // instead of scrolling screen-by-screen. normalizeText collapses whitespace.
    const root = document.body || document.documentElement;
    return normalizeText((root && (root.innerText || root.textContent)) || "", limit);
  }

  function mainText(limit = 5000) {
    const selectors = ["main", "article", "[role='main']", "#content", "#main", "#b_content", "#b_results", ".content", ".main"];
    const chunks = [];
    let total = 0;
    for (const selector of selectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (!isVisible(el)) continue;
        const text = normalizeText(el.innerText || el.textContent || "", limit - total);
        if (!text || chunks.includes(text)) continue;
        chunks.push(text);
        total += text.length + 1;
        if (total >= limit) return chunks.join("\n").slice(0, limit);
      }
    }
    return chunks.join("\n").slice(0, limit);
  }

  function safeCssEscape(value) {
    const text = String(value || "");
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(text);
    return text.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function semanticPrefix(el) {
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();
    const type = tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : "";
    const nestedInputType = el.querySelector?.("input")?.getAttribute("type")?.toLowerCase() || "";
    if (tag === "label" && (nestedInputType === "checkbox" || nestedInputType === "radio")) return nestedInputType;
    if (tag === "a" || el.closest("a[href]")) return "link";
    if (tag === "button" || role === "button" || ["button", "submit", "reset", "image"].includes(type)) return "btn";
    if (tag === "input") {
      if (type === "checkbox" || type === "radio") return type;
      return "input";
    }
    if (tag === "textarea") return "textarea";
    if (tag === "select" || role === "combobox" || role === "listbox") return "select";
    if (el.isContentEditable) return "editable";
    if (tag === "summary") return "summary";
    if (role) return role.replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 24) || "el";
    return "el";
  }

  function associatedLabel(el) {
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = labelledBy
        .split(/\s+/)
        .map((id) => document.getElementById(id)?.innerText || document.getElementById(id)?.textContent || "")
        .filter(Boolean)
        .join(" ");
      if (normalizeText(text, 160)) return normalizeText(text, 160);
    }
    if (el.id) {
      const label = document.querySelector(`label[for="${safeCssEscape(el.id)}"]`);
      if (label) return normalizeText(label.innerText || label.textContent || "", 160);
    }
    const parentLabel = el.closest("label");
    if (parentLabel) return normalizeText(parentLabel.innerText || parentLabel.textContent || "", 160);
    return "";
  }

  function compactSelector(el) {
    const tag = el.tagName.toLowerCase();
    if (el.id) return `${tag}#${safeCssEscape(el.id)}`;
    const name = el.getAttribute("name");
    if (name && ["input", "textarea", "select", "button"].includes(tag)) return `${tag}[name="${safeCssEscape(name)}"]`;
    const className = String(el.className || "")
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 3)
      .map((item) => `.${safeCssEscape(item)}`)
      .join("");
    return `${tag}${className}`;
  }

  function contextContainer(el) {
    let fallback = null;
    for (let current = el.parentElement, depth = 0; current && current !== document.body && depth < 8; current = current.parentElement, depth += 1) {
      const tag = current.tagName.toLowerCase();
      const role = (current.getAttribute("role") || "").toLowerCase();
      const className = String(current.className || "").toLowerCase();
      const text = normalizeText(current.innerText || current.textContent || "", 900);
      if (!text) continue;
      if (!fallback && text.length <= 900) fallback = current;
      if (
        ["form", "article", "li", "tr", "td", "section"].includes(tag) ||
        ["article", "listitem", "row", "search", "main", "menuitem", "tab"].includes(role) ||
        className.includes("result") ||
        className.includes("card") ||
        className.includes("item") ||
        className.includes("content") ||
        className.includes("menu")
      ) {
        return current;
      }
    }
    return fallback || el.parentElement || el;
  }

  function containerText(el) {
    const container = contextContainer(el);
    return normalizeText(container?.innerText || container?.textContent || "", 1100);
  }

  function formContext(el) {
    const form = el.closest("form");
    if (!form) return "";
    const controls = Array.from(form.querySelectorAll("input, textarea, select, button"))
      .filter(isVisible)
      .slice(0, 16)
      .map((control) => {
        const tag = control.tagName.toLowerCase();
        const type = tag === "input" ? (control.getAttribute("type") || "text").toLowerCase() : "";
        const label = associatedLabel(control) || control.getAttribute("placeholder") || control.getAttribute("aria-label") || control.getAttribute("name") || textOf(control);
        return normalizeText([tag, type, label].filter(Boolean).join(":"), 120);
      })
      .filter(Boolean);
    const formText = normalizeText(form.innerText || form.textContent || "", 600);
    return normalizeText([formText, controls.length ? `controls: ${controls.join("; ")}` : ""].filter(Boolean).join(" | "), 1000);
  }

  function semanticAttributes(el, elementIds) {
    const attrs = [];
    const agentId = elementIds.get(el);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role");
    const type = tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : "";
    const name = el.getAttribute("name");
    const placeholder = el.getAttribute("placeholder");
    const ariaLabel = el.getAttribute("aria-label");
    const href = el.closest("a[href]") instanceof HTMLAnchorElement ? el.closest("a[href]").href : "";
    if (agentId) attrs.push(`data-agent-id="${agentId}"`);
    if (agentId) attrs.push(`data-agent-desc="${normalizeText(elementDescription(el), 180)}"`);
    if (role) attrs.push(`role="${normalizeText(role, 60)}"`);
    if (type) attrs.push(`type="${normalizeText(type, 60)}"`);
    if (name) attrs.push(`name="${normalizeText(name, 80)}"`);
    if (placeholder) attrs.push(`placeholder="${normalizeText(placeholder, 120)}"`);
    if (ariaLabel) attrs.push(`aria-label="${normalizeText(ariaLabel, 120)}"`);
    if (href) attrs.push(`href="${normalizeText(href, 180)}"`);
    return attrs.length ? ` ${attrs.join(" ")}` : "";
  }

  function semanticOwnText(el, elementIds) {
    const tag = el.tagName.toLowerCase();
    if (elementIds.has(el) || ["h1", "h2", "h3", "h4", "p", "li", "label", "summary"].includes(tag)) {
      return normalizeText(el.innerText || el.textContent || "", elementIds.has(el) ? 180 : 260);
    }
    return "";
  }

  function shouldIncludeSemanticNode(el, elementIds) {
    if (!isVisible(el)) return false;
    if (elementIds.has(el)) return true;
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (["main", "article", "section", "form", "nav", "header", "footer", "h1", "h2", "h3", "h4", "p", "li", "label", "summary"].includes(tag)) {
      return Boolean(normalizeText(el.innerText || el.textContent || "", 80));
    }
    return ["main", "article", "list", "listitem", "search", "form", "navigation", "button", "link", "textbox", "menuitem", "tab"].includes(role);
  }

  function buildSemanticTree(elementIds, limit = 14000) {
    const root = document.querySelector("main, article, [role='main'], #content, #main, #b_content, #b_results") || document.body || document.documentElement;
    const lines = [];
    let chars = 0;
    let visited = 0;
    function addLine(line) {
      if (!line || chars >= limit || lines.length >= 420) return false;
      const next = line.slice(0, Math.max(0, limit - chars));
      lines.push(next);
      chars += next.length + 1;
      return chars < limit && lines.length < 420;
    }
    function walk(node, depth) {
      if (!node || chars >= limit || visited >= 900 || !(node instanceof Element)) return;
      visited += 1;
      const tag = node.tagName.toLowerCase();
      if (["script", "style", "noscript", "svg", "canvas", "iframe"].includes(tag)) return;
      const include = shouldIncludeSemanticNode(node, elementIds);
      const nextDepth = include ? depth + 1 : depth;
      if (include) {
        const text = semanticOwnText(node, elementIds);
        if (!addLine(`${"  ".repeat(Math.min(depth, 8))}<${tag}${semanticAttributes(node, elementIds)}>${text ? ` ${text}` : ""}`)) return;
      }
      for (const child of Array.from(node.children).slice(0, 80)) {
        walk(child, nextDepth);
        if (chars >= limit || lines.length >= 420) break;
      }
    }
    walk(root, 0);
    return lines.join("\n").slice(0, limit);
  }

  function isLikelyClickable(el) {
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();
    const className = String(el.className || "").toLowerCase();
    const id = String(el.id || "").toLowerCase();
    const style = window.getComputedStyle(el);
    if (["a", "button", "input", "textarea", "select", "summary", "label"].includes(tag)) return true;
    if (el.isContentEditable || el.getAttribute("tabindex") !== null || el.getAttribute("onclick")) return true;
    if (["button", "link", "menuitem", "tab", "option", "checkbox", "radio", "switch", "textbox"].includes(role)) return true;
    if (style.cursor === "pointer") return true;
    return /(^|[-_\s])(btn|button|link|menu|menuitem|nav|tab|item|option|card|row|cell)([-_\s]|$)/i.test(`${className} ${id}`);
  }

  function elementState(el, checkedTarget) {
    const states = [];
    const ariaChecked = el.getAttribute("aria-checked");
    const ariaSelected = el.getAttribute("aria-selected");
    const dataChecked = el.getAttribute("data-checked");
    const className = String(el.className || "").toLowerCase();

    let checked = null;
    if (checkedTarget) checked = Boolean(checkedTarget.checked);
    if (ariaChecked === "true" || dataChecked === "true") checked = true;
    if (ariaChecked === "false" || dataChecked === "false") checked = false;
    if (/(^|[-_\s])(checked|is-checked|selected|active)([-_\s]|$)/i.test(className)) checked = true;

    if (checked === true) states.push("checked");
    if (checked === false && (checkedTarget || ariaChecked || dataChecked)) states.push("unchecked");
    if (ariaSelected === "true") states.push("selected");
    if (ariaSelected === "false") states.push("unselected");
    if (/(^|[-_\s])(disabled|is-disabled)([-_\s]|$)/i.test(className)) states.push("disabled");
    return { checked, state: states.join(" ") };
  }

  function primaryName(el) {
    const parts = [
      associatedLabel(el),
      textOf(el),
      el.getAttribute("placeholder") || "",
      el.getAttribute("aria-label") || "",
      el.getAttribute("title") || "",
      el.getAttribute("name") || "",
      el.getAttribute("alt") || ""
    ]
      .map((part) => normalizeText(part, 180))
      .filter(Boolean);
    return Array.from(new Set(parts))[0] || "";
  }

  function elementDescription(el) {
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();
    const type = tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : "";
    const href = el.closest("a[href]") instanceof HTMLAnchorElement ? el.closest("a[href]").href : "";
    const name = primaryName(el);
    const context = normalizeText(containerText(el), 260);
    const blob = normalizeText([name, context, href].join(" "), 520).toLowerCase();
    let kind = "interactive element";
    if (tag === "a" || href) kind = "link";
    else if (tag === "button" || role === "button" || ["button", "submit", "reset", "image"].includes(type)) kind = type === "submit" ? "submit button" : "button";
    else if (tag === "input") {
      if (type === "checkbox") kind = "checkbox";
      else if (type === "radio") kind = "radio option";
      else if (type === "password") kind = "password input";
      else if (type === "search" || /search|搜索|query|dataset|model/.test(blob)) kind = "search input";
      else if (/email|邮箱/.test(blob) || type === "email") kind = "email input";
      else if (/phone|mobile|手机号|手机|电话/.test(blob) || type === "tel") kind = "phone input";
      else if (/code|验证码|verification/.test(blob)) kind = "verification-code input";
      else kind = "text input";
    } else if (tag === "textarea") kind = "multiline text input";
    else if (tag === "select" || role === "combobox" || role === "listbox") kind = "select control";
    else if (role) kind = `${role} control`;

    const qualifiers = [];
    if (/dataset|datasets|数据集/.test(blob)) qualifiers.push("dataset-related");
    if (/download|下载|raw|resolve|file|files|文件|data/.test(blob)) qualifiers.push("resource/file-related");
    if (/login|signin|登录|账号|账户|account/.test(blob)) qualifiers.push("login/account-related");
    if (/search|搜索|query/.test(blob)) qualifiers.push("search-related");
    const label = name || context.slice(0, 100) || href;
    return normalizeText([kind, qualifiers.length ? `(${qualifiers.join(", ")})` : "", label ? `- ${label}` : ""].join(" "), 260);
  }

  function actionHintFor(el) {
    const description = elementDescription(el).toLowerCase();
    const tag = el.tagName.toLowerCase();
    const type = tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : "";
    if (description.includes("search input")) return "type concise keywords here, then press Enter or click the nearby search button";
    if (description.includes("password input")) return "type the password only when safety allows password input";
    if (description.includes("input")) return "type the requested value if this field matches the task";
    if (description.includes("checkbox") || description.includes("radio option")) return "click to toggle/select this option if required";
    if (description.includes("submit button") || type === "submit") return "click to submit the current form after required fields are filled";
    if (description.includes("link") || tag === "a") return "click to open this visible page link instead of editing the URL directly";
    if (description.includes("button")) return "click if this visible button advances the task";
    return "interact only if this visible control matches the current task";
  }

  function buildElementRecord(el, id, legacyIndex) {
    const rect = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const type = tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : "";
    const nestedInput = el.querySelector?.("input[type='checkbox'], input[type='radio']") || null;
    const checkedTarget = tag === "input" ? el : nestedInput;
    const closestLink = el.closest("a[href]");
    const valueElement = el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement;
    const rawValue = valueElement ? String(el.value || "") : "";
    const state = elementState(el, checkedTarget);
    window.__pwAgentElements[id] = el;
    window.__pwAgentElements["el_" + legacyIndex] = el;
    return {
      id,
      legacyId: "el_" + legacyIndex,
      tag,
      role: el.getAttribute("role") || "",
      type,
      text: textOf(el),
      placeholder: el.getAttribute("placeholder") || "",
      ariaLabel: el.getAttribute("aria-label") || "",
      name: el.getAttribute("name") || "",
      title: el.getAttribute("title") || "",
      alt: el.getAttribute("alt") || "",
      label: associatedLabel(el),
      description: elementDescription(el),
      actionHint: actionHintFor(el),
      selector: compactSelector(el),
      value: type === "password" ? "" : rawValue.slice(0, 300),
      hasValue: rawValue.length > 0,
      href: closestLink instanceof HTMLAnchorElement ? closestLink.href : "",
      checked: state.checked,
      state: state.state,
      disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true"),
      nearbyText: nearbyText(el, 700),
      containerText: containerText(el),
      formContext: formContext(el),
      x: Math.round(rect.left),
      y: Math.round(rect.top),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      isInViewport: inViewport(rect)
    };
  }

  const selector = [
    "a[href]",
    "button",
    "input",
    "label",
    "textarea",
    "select",
    "[aria-checked]",
    "[role]",
    "[tabindex]",
    "summary",
    "[contenteditable='true']"
  ].join(",");

  const directCandidates = Array.from(document.querySelectorAll(selector));
  const clickableCandidates = Array.from(document.querySelectorAll("body *"))
    .filter((el) => isLikelyClickable(el))
    .filter((el) => normalizeText(el.innerText || el.textContent || el.getAttribute("aria-label") || el.getAttribute("title") || "", 220));
  const candidates = Array.from(new Set([...directCandidates, ...clickableCandidates]))
    .filter(isVisible)
    .filter((el) => {
      const rect = el.getBoundingClientRect();
      return rect.width >= 2 && rect.height >= 2;
    })
    .slice(0, 200);

  const idCounts = {};
  const elementIds = new WeakMap();
  const elements = candidates.map((el, index) => {
    const prefix = semanticPrefix(el);
    idCounts[prefix] = (idCounts[prefix] || 0) + 1;
    const record = buildElementRecord(el, `${prefix}_${idCounts[prefix]}`, index + 1);
    elementIds.set(el, record.id);
    return record;
  });
  const references = Object.fromEntries(
    elements.map((element) => [
      element.id,
      {
        tag: element.tag,
        role: element.role,
        type: element.type,
        text: element.text,
        placeholder: element.placeholder,
        ariaLabel: element.ariaLabel,
        name: element.name,
        label: element.label,
        description: element.description,
        actionHint: element.actionHint,
        href: element.href,
        selector: element.selector
      }
    ])
  );

  const viewportText = getViewportText(10000);
  const pageTextPreview = mainText(5000);
  const fullText = fullDocText(40000);
  const semanticTree = buildSemanticTree(elementIds);
  const scrollHeight = Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0, window.innerHeight);
  const scrollY = Math.round(window.scrollY || document.documentElement.scrollTop || 0);
  const viewportHeight = window.innerHeight;

  return {
    url: window.location.href,
    title: document.title || "",
    visibleText: [viewportText, pageTextPreview].filter(Boolean).join("\n\n--- Page body preview ---\n").slice(0, 22000),
    viewportText,
    pageTextPreview,
    fullText,
    fullTextLength: fullText.length,
    semanticTree,
    cleanedHtml: semanticTree,
    observedText: viewportText,
    observedTextLength: viewportText.length,
    scrollY,
    scroll: {
      scrollY,
      scrollHeight,
      viewportHeight,
      currentRange: { start: scrollY, end: Math.min(scrollY + viewportHeight, scrollHeight) },
      observedRanges: [{ start: scrollY, end: Math.min(scrollY + viewportHeight, scrollHeight) }],
      observedCoverage: scrollHeight ? Math.min(1, viewportHeight / scrollHeight) : 1,
      canScrollDown: scrollY + viewportHeight < scrollHeight - 20,
      canScrollUp: scrollY > 20,
      scroller: "document"
    },
    viewport: { width: window.innerWidth, height: window.innerHeight },
    elements,
    references
  };
}
"""


ACTION_SCRIPT = r"""
async (action) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const elements = window.__pwAgentElements || {};
  const target = action.target_id ? elements[action.target_id] : null;

  function dispatchInputEvents(el, data = "") {
    try {
      el.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data }));
    } catch (_) {
      // Older pages may not support InputEvent construction.
    }
    try {
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data }));
    } catch (_) {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function setNativeValue(el, value) {
    const ownDescriptor = Object.getOwnPropertyDescriptor(el, "value");
    const prototype = Object.getPrototypeOf(el);
    const prototypeDescriptor = Object.getOwnPropertyDescriptor(prototype, "value");
    if (prototypeDescriptor?.set && ownDescriptor?.set !== prototypeDescriptor.set) {
      prototypeDescriptor.set.call(el, value);
    } else if (ownDescriptor?.set) {
      ownDescriptor.set.call(el, value);
    } else if (prototypeDescriptor?.set) {
      prototypeDescriptor.set.call(el, value);
    } else {
      el.value = value;
    }
  }

  function dispatchTypingKeys(el, text) {
    for (const char of String(text || "")) {
      const init = { key: char, code: "", bubbles: true, cancelable: true };
      el.dispatchEvent(new KeyboardEvent("keydown", init));
      el.dispatchEvent(new KeyboardEvent("keypress", init));
      el.dispatchEvent(new KeyboardEvent("keyup", init));
    }
  }

  if (action.action === "click") {
    if (!target) throw new Error("Target element not found: " + action.target_id);
    const clickTarget = target instanceof HTMLAnchorElement ? target : target.closest("a[href]") || target;
    const nestedChoice = target.querySelector?.("input[type='checkbox'], input[type='radio']") || null;
    const beforeChecked = nestedChoice ? nestedChoice.checked : null;
    clickTarget.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    await sleep(150);
    clickTarget.focus({ preventScroll: true });
    clickTarget.click();
    if (nestedChoice && nestedChoice.checked === beforeChecked) {
      nestedChoice.checked = nestedChoice.type === "radio" ? true : !beforeChecked;
      nestedChoice.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      nestedChoice.dispatchEvent(new Event("input", { bubbles: true }));
      nestedChoice.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return { ok: true, message: "clicked" };
  }

  if (action.action === "type") {
    if (!target) throw new Error("Target element not found: " + action.target_id);
    target.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    await sleep(100);
    target.focus({ preventScroll: true });
    target.click?.();
    if (target.isContentEditable) {
      target.textContent = action.text || "";
      dispatchInputEvents(target, action.text || "");
      return { ok: true, message: "typed", hasValue: Boolean(target.textContent), valueLength: String(target.textContent || "").length };
    }
    setNativeValue(target, "");
    dispatchInputEvents(target, "");
    dispatchTypingKeys(target, action.text || "");
    setNativeValue(target, action.text || "");
    dispatchInputEvents(target, action.text || "");
    await sleep(80);
    const currentValue = String(target.value || "");
    const expected = String(action.text || "");
    if (currentValue !== expected) {
      return { ok: false, message: "type_value_not_persisted", hasValue: Boolean(currentValue), valueLength: currentValue.length, expectedLength: expected.length };
    }
    return { ok: true, message: "typed", hasValue: Boolean(currentValue), valueLength: currentValue.length };
  }

  if (action.action === "press") {
    const key = action.key || "Enter";
    const targetEl = document.activeElement || document.body;
    targetEl.dispatchEvent(new KeyboardEvent("keydown", { key, code: key, bubbles: true, cancelable: true }));
    targetEl.dispatchEvent(new KeyboardEvent("keyup", { key, code: key, bubbles: true, cancelable: true }));
    if (key === "Enter" && targetEl.form) targetEl.form.requestSubmit();
    return { ok: true, message: "pressed " + key };
  }

  if (action.action === "scroll") {
    const amount = Number(action.amount || 0);
    const before = Math.round(window.scrollY || document.documentElement.scrollTop || 0);
    window.scrollBy({ top: amount, left: 0, behavior: "auto" });
    await sleep(300);
    const after = Math.round(window.scrollY || document.documentElement.scrollTop || 0);
    const delta = after - before;
    return { ok: Math.abs(delta) >= 20, message: Math.abs(delta) >= 20 ? "scrolled" : "scroll_no_progress", before, after, delta };
  }

  if (action.action === "wait") {
    await sleep(Math.min(Math.max(Number(action.ms || 1000), 0), 10000));
    return { ok: true, message: "waited" };
  }

  throw new Error("Unsupported DOM action: " + action.action);
}
"""


def find_chrome_executable() -> str:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        str(Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def cdp_port_from_url(cdp_url: str) -> int:
    parsed = urlparse(cdp_url or "")
    if parsed.port:
        return int(parsed.port)
    return 443 if parsed.scheme == "https" else 80


def cdp_host_from_url(cdp_url: str) -> str:
    parsed = urlparse(cdp_url or "")
    return parsed.hostname or "127.0.0.1"


def cdp_version_url(cdp_url: str) -> str:
    return cdp_url.rstrip("/") + "/json/version"


async def is_cdp_available(cdp_url: str, timeout: float = 0.8) -> bool:
    def probe() -> bool:
        try:
            with urlopen(cdp_version_url(cdp_url), timeout=timeout) as response:
                return response.status == 200
        except Exception:
            return False

    return await asyncio.to_thread(probe)


async def wait_for_cdp(cdp_url: str, timeout_ms: int) -> bool:
    deadline = time.monotonic() + max(timeout_ms, 1000) / 1000
    while time.monotonic() < deadline:
        if await is_cdp_available(cdp_url):
            return True
        await asyncio.sleep(0.25)
    return False


def viewport_mode(browser_config: dict) -> str:
    viewport = browser_config.get("viewport")
    if isinstance(viewport, str):
        value = viewport.strip().lower()
        if value in {"auto", "fullscreen", "maximized", "maximize"}:
            return value
    if isinstance(viewport, dict):
        width = str(viewport.get("width", "")).strip().lower()
        height = str(viewport.get("height", "")).strip().lower()
        if "fullscreen" in {width, height}:
            return "fullscreen"
        if "maximized" in {width, height} or "maximize" in {width, height}:
            return "maximized"
        if width == "auto" or height == "auto":
            return "auto"
    return "fixed"


def resolve_viewport(browser_config: dict) -> dict | None:
    if viewport_mode(browser_config) in {"auto", "fullscreen", "maximized", "maximize"}:
        return None
    viewport = browser_config.get("viewport") or {"width": 1280, "height": 900}
    if not isinstance(viewport, dict):
        return {"width": 1280, "height": 900}
    try:
        return {"width": int(viewport.get("width", 1280)), "height": int(viewport.get("height", 900))}
    except (TypeError, ValueError):
        return {"width": 1280, "height": 900}


def launch_cdp_browser_process(browser_config: dict, cdp_url: str) -> subprocess.Popen:
    executable = browser_config.get("executable_path") or browser_config.get("chrome_path") or find_chrome_executable()
    if not executable:
        raise RuntimeError("Chrome/Edge executable was not found. Set browser.executable_path in config.json.")

    port = int(browser_config.get("remote_debugging_port") or cdp_port_from_url(cdp_url) or 9222)
    host = browser_config.get("remote_debugging_address") or cdp_host_from_url(cdp_url)
    user_data_dir = (
        browser_config.get("user_data_dir")
        or browser_config.get("userDataDir")
        or str(Path(tempfile.gettempdir()) / "cdpagent-memory-chrome-profile")
    )
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)

    command = [
        str(executable),
        f"--remote-debugging-port={port}",
        f"--remote-debugging-address={host}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if viewport_mode(browser_config) in {"fullscreen", "maximized", "maximize"}:
        command.append("--start-maximized")
    start_url = browser_config.get("start_url")
    if start_url:
        command.append(start_url)
    command.extend(str(arg) for arg in browser_config.get("cdp_launch_args", []) if arg)

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)


class BrowserController:
    def __init__(
        self,
        browser: Browser,
        page: Page,
        focus_page: bool = True,
        downloads_path: str | None = None,
        observation_empty_retries: int = 3,
        observation_empty_retry_ms: int = 350,
        new_page_adoption_timeout_ms: int = 3000,
        organize_downloads_by_resource: bool = True,
    ):
        self.browser = browser
        self.page = page
        self.context = page.context
        self.connected_over_cdp = False
        self._cdp_process: subprocess.Popen | None = None
        self._close_cdp_process = False
        self.focus_page = focus_page
        self.downloads_path = str(downloads_path or Path(__file__).resolve().parents[1] / "downloads")
        self.organize_downloads_by_resource = bool(organize_downloads_by_resource)
        self.active_resource_name: str | None = None
        self.observation_empty_retries = max(0, int(observation_empty_retries))
        self.observation_empty_retry_ms = max(0, int(observation_empty_retry_ms))
        self.new_page_adoption_timeout_ms = max(0, int(new_page_adoption_timeout_ms))
        self.observed_text_by_url: dict[str, str] = {}
        self.observed_ranges_by_url: dict[str, list[dict]] = {}
        self._page_ids: dict[Page, str] = {}
        self._next_page_id = 1
        self.page_id(self.page)

    @classmethod
    async def launch(cls, config: dict) -> "BrowserController":
        playwright = await async_playwright().start()
        browser_config = config.get("browser", {})
        mode = str(browser_config.get("connection") or browser_config.get("mode") or "launch").lower()
        viewport = resolve_viewport(browser_config)
        is_maximized = viewport_mode(browser_config) in {"fullscreen", "maximized", "maximize"}
        focus_page = bool(browser_config.get("focus_page", True))
        downloads_path = browser_config.get("downloads_path") or browser_config.get("download_dir")
        if downloads_path:
            Path(downloads_path).mkdir(parents=True, exist_ok=True)
        context_options = {"accept_downloads": True}
        if viewport is None:
            context_options["no_viewport"] = True
        else:
            context_options["viewport"] = viewport
        # NOTE: downloads_path is a launch() argument, NOT a new_context() kwarg.
        # It is applied on chromium.launch(...) below (launch mode only).

        if mode == "cdp":
            cdp_url = browser_config.get("cdp_url") or browser_config.get("cdpUrl") or "http://127.0.0.1:9222"
            cdp_process = None
            if not await is_cdp_available(cdp_url):
                if bool(browser_config.get("auto_start_cdp", True)):
                    print(f"CDP endpoint is not available; starting browser for {cdp_url}")
                    cdp_process = launch_cdp_browser_process(browser_config, cdp_url)
                    timeout_ms = int(browser_config.get("cdp_startup_timeout_ms", 10000))
                    if not await wait_for_cdp(cdp_url, timeout_ms):
                        raise RuntimeError(f"Started browser but CDP endpoint did not become available: {cdp_url}")
                else:
                    raise RuntimeError(f"CDP endpoint is not available: {cdp_url}")
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context(**context_options)
            page = next((candidate for candidate in context.pages if candidate.url.startswith(("http://", "https://"))), None)
            if page is None:
                page = context.pages[0] if context.pages else await context.new_page()
            if viewport is not None:
                try:
                    await page.set_viewport_size(viewport)
                except Exception:
                    pass
            start_url = browser_config.get("start_url")
            if start_url and not page.url.startswith(("http://", "https://")):
                await page.goto(start_url, wait_until="domcontentloaded")
            controller = cls(
                browser,
                page,
                focus_page=focus_page,
                downloads_path=downloads_path,
                observation_empty_retries=int(browser_config.get("observation_empty_retries", 3)),
                observation_empty_retry_ms=int(browser_config.get("observation_empty_retry_ms", 350)),
                new_page_adoption_timeout_ms=int(browser_config.get("new_page_adoption_timeout_ms", 3000)),
                organize_downloads_by_resource=bool(browser_config.get("organize_downloads_by_resource", True)),
            )
            controller.connected_over_cdp = True
            controller._cdp_process = cdp_process
            controller._close_cdp_process = bool(browser_config.get("close_auto_started_cdp", False))
        else:
            launch_kwargs = {
                "headless": bool(browser_config.get("headless", False)),
                "slow_mo": int(browser_config.get("slow_mo", 300)),
                "args": ["--start-maximized"] if is_maximized else None,
            }
            if downloads_path:
                launch_kwargs["downloads_path"] = str(downloads_path)
            browser = await playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(**context_options)
            page = await context.new_page()
            start_url = browser_config.get("start_url") or "https://www.bing.com"
            try:
                await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                # Some official sites (notably flk.npc.gov.cn) insert a WZWS
                # challenge/redirect that can outlive Playwright's navigation
                # timeout even though the page continues loading successfully.
                # Keep the browser session alive so the agent can observe/recover.
                try:
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass
            controller = cls(
                browser,
                page,
                focus_page=focus_page,
                downloads_path=downloads_path,
                observation_empty_retries=int(browser_config.get("observation_empty_retries", 3)),
                observation_empty_retry_ms=int(browser_config.get("observation_empty_retry_ms", 350)),
                new_page_adoption_timeout_ms=int(browser_config.get("new_page_adoption_timeout_ms", 3000)),
                organize_downloads_by_resource=bool(browser_config.get("organize_downloads_by_resource", True)),
            )

        controller._playwright = playwright
        await controller.bring_page_to_front()
        return controller

    async def bring_page_to_front(self) -> None:
        if not self.focus_page:
            return
        try:
            await self.page.bring_to_front()
        except Exception:
            pass

    def browser_pages(self) -> list[Page]:
        return [
            page
            for context in self.browser.contexts
            for page in context.pages
            if not page.is_closed()
        ]

    def page_id(self, page: Page) -> str:
        if page not in self._page_ids:
            self._page_ids[page] = f"page_{self._next_page_id}"
            self._next_page_id += 1
        return self._page_ids[page]

    def resolve_page_target(self, action: dict) -> Page | None:
        pages = self.browser_pages()
        page_id = str(action.get("page_id") or "").strip()
        if page_id:
            return next((page for page in pages if self.page_id(page) == page_id), None)
        try:
            page_index = int(action.get("page_index"))
        except (TypeError, ValueError):
            return None
        return pages[page_index] if 0 <= page_index < len(pages) else None

    async def adopt_new_page_if_any(self, known_pages: set[Page], timeout_ms: int | None = None) -> Page | None:
        deadline = time.monotonic() + max(
            0,
            self.new_page_adoption_timeout_ms if timeout_ms is None else int(timeout_ms),
        ) / 1000
        new_pages: list[Page] = []
        while True:
            new_pages = [page for page in self.browser_pages() if page not in known_pages]
            if new_pages or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)
        if not new_pages:
            return None
        self.page = new_pages[-1]
        self.context = self.page.context
        self.page_id(self.page)
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        await self.bring_page_to_front()
        return self.page

    async def page_context(self) -> dict:
        pages = []
        active_index = None
        active_page_id = None
        for index, page in enumerate(self.browser_pages()):
            if page == self.page:
                active_index = index
                active_page_id = self.page_id(page)
            try:
                title = await page.title()
            except Exception:
                title = ""
            pages.append(
                {
                    "pageId": self.page_id(page),
                    "index": index,
                    "title": (title or "")[:180],
                    "url": (page.url or "")[:700],
                    "active": page == self.page,
                    "currentTask": page == self.page,
                    "closed": page.is_closed(),
                }
            )
        return {
            "totalPages": len(pages),
            "activePageIndex": active_index,
            "taskPageIndex": active_index,
            "activePageId": active_page_id,
            "taskPageId": active_page_id,
            "connectedOverCdp": bool(self.connected_over_cdp),
            "pages": pages[:30],
        }

    def merge_observed_text(self, url: str, text: str) -> str:
        previous = self.observed_text_by_url.get(url, "")
        normalized = " ".join((text or "").split())
        if not normalized:
            return previous
        if normalized[:160] not in previous:
            combined = f"{previous}\n--- viewport ---\n{normalized}" if previous else normalized
            self.observed_text_by_url[url] = combined[-40000:]
        return self.observed_text_by_url.get(url, "")

    def merge_observed_ranges(self, url: str, current_range: dict) -> list[dict]:
        ranges = [*self.observed_ranges_by_url.get(url, []), current_range]
        clean_ranges = [
            {"start": int(item.get("start", 0)), "end": int(item.get("end", 0))}
            for item in ranges
            if item and int(item.get("end", 0)) >= int(item.get("start", 0))
        ]
        clean_ranges.sort(key=lambda item: item["start"])
        merged: list[dict] = []
        for item in clean_ranges:
            if not merged or item["start"] > merged[-1]["end"] + 80:
                merged.append(dict(item))
            else:
                merged[-1]["end"] = max(merged[-1]["end"], item["end"])
        self.observed_ranges_by_url[url] = merged[-20:]
        return self.observed_ranges_by_url[url]

    @staticmethod
    def is_transient_empty_observation(observation: dict) -> bool:
        url = str(observation.get("url") or "")
        if not url.startswith(("http://", "https://")):
            return False
        return not (
            observation.get("elements")
            or str(observation.get("viewportText") or "").strip()
            or str(observation.get("pageTextPreview") or "").strip()
            or str(observation.get("semanticTree") or "").strip()
        )

    async def evaluate_observation(self) -> dict:
        try:
            return await self.page.evaluate(OBSERVE_SCRIPT)
        except Exception as exc:
            return {
                "url": self.page.url,
                "title": "Browser Error Page",
                "visibleText": f"Browser Error: {exc}",
                "viewportText": f"Browser Error: {exc}",
                "pageTextPreview": "",
                "observedText": "",
                "observedTextLength": 0,
                "scroll": {"canScrollDown": False, "viewportHeight": 800, "observedCoverage": 1},
                "viewport": {"width": 0, "height": 0},
                "elements": [],
            }

    async def observe(self) -> dict:
        await self.bring_page_to_front()
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=1500)
        except Exception:
            pass
        observation = await self.evaluate_observation()
        initial_empty = self.is_transient_empty_observation(observation)
        empty_retries = 0
        while (
            self.is_transient_empty_observation(observation)
            and empty_retries < self.observation_empty_retries
        ):
            empty_retries += 1
            await self.page.wait_for_timeout(self.observation_empty_retry_ms)
            observation = await self.evaluate_observation()
        if initial_empty:
            observation["captureMeta"] = {
                "initialEmpty": True,
                "emptyRetries": empty_retries,
                "stabilizedAfterRetry": not self.is_transient_empty_observation(observation),
            }
        observed_text = self.merge_observed_text(observation.get("url", ""), observation.get("viewportText", ""))
        scroll = observation.get("scroll") or {}
        current_range = scroll.get("currentRange") or {}
        observed_ranges = self.merge_observed_ranges(observation.get("url", ""), current_range)
        scroll_height = int(scroll.get("scrollHeight") or 0)
        observed_pixels = sum(max(0, int(item.get("end", 0)) - int(item.get("start", 0))) for item in observed_ranges)
        if scroll_height > 0:
            scroll["observedRanges"] = observed_ranges
            scroll["observedCoverage"] = min(1, observed_pixels / scroll_height)
            observation["scroll"] = scroll
        observation["observedText"] = observed_text
        observation["observedTextLength"] = len(observed_text)
        observation["visibleText"] = "\n\n--- Observed text ---\n".join(
            part
            for part in [
                observation.get("viewportText", ""),
                observation.get("pageTextPreview", ""),
                observed_text,
            ]
            if part
        )[:22000]
        return observation

    async def capture_screenshot_for_vision(self) -> dict | None:
        await self.bring_page_to_front()
        try:
            image = await self.page.screenshot(type="jpeg", quality=65, full_page=False)
        except Exception:
            return None
        data_url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
        return {
            "dataUrl": data_url,
            "mimeType": "image/jpeg",
            "scope": "current_viewport",
        }

    def resource_directories(
        self,
        url: str = "",
        filename: str | None = None,
        resource_name: str | None = None,
    ) -> tuple[Path, Path, str]:
        root = Path(self.downloads_path).resolve()
        if not self.organize_downloads_by_resource:
            root.mkdir(parents=True, exist_ok=True)
            return root, root, ""
        if not self.active_resource_name:
            self.active_resource_name = safe_resource_name(resource_name) if resource_name else infer_resource_name(url, filename)
        resource_root = root / self.active_resource_name
        data_dir = resource_root / DATA_DIR_NAME
        analysis_dir = resource_root / ANALYSIS_DIR_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        analysis_dir.mkdir(parents=True, exist_ok=True)
        return data_dir, analysis_dir, self.active_resource_name

    def artifact_directories(self) -> tuple[Path, Path]:
        if self.organize_downloads_by_resource and self.active_resource_name:
            resource_root = Path(self.downloads_path).resolve() / self.active_resource_name
            return resource_root / DATA_DIR_NAME, resource_root / ANALYSIS_DIR_NAME
        root = Path(self.downloads_path).resolve()
        return root, root

    async def download_url(self, url: str, filename: str | None = None, resource_name: str | None = None) -> dict:
        parsed = urlparse(url or "")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("download only supports http/https URLs")
        if parsed.hostname == "flk.npc.gov.cn" and parsed.path.rstrip("/") == "/detail":
            query = parse_qs(parsed.query)
            bbbs = (query.get("id") or [""])[0]
            if bbbs:
                try:
                    payload = await self.page.evaluate(
                        """async (bbbs) => {
                          const response = await fetch(`/law-search/download/pc?format=docx&bbbs=${encodeURIComponent(bbbs)}`);
                          return await response.json();
                        }""",
                        bbbs,
                    )
                    signed_url = ((payload or {}).get("data") or {}).get("url") or ""
                    if signed_url:
                        title = unquote((query.get("title") or [""])[0]).strip()
                        title = re.sub(r"[\\/:*?\"<>|]+", "", title) or Path(urlparse(signed_url).path).stem
                        return await self.download_url(signed_url, f"{title}.docx", resource_name)
                except Exception:
                    pass
        safe_name = filename or Path(parsed.path).name or "downloaded_resource"
        safe_name = "".join(char for char in safe_name if char.isalnum() or char in "._- ").strip() or "downloaded_resource"
        target_dir, analysis_dir, resolved_resource_name = self.resource_directories(url, safe_name, resource_name)
        target_path = target_dir / safe_name

        def fetch() -> bytes:
            # Send a real UA — some official sites reject the default urllib agent.
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urlopen(req, timeout=30) as response:
                return response.read()

        data = await asyncio.to_thread(fetch)
        # Guard against JS/session-gated downloads (e.g. flk.npc.gov.cn) that hand
        # urllib a small HTML placeholder/error page instead of the real file. If the
        # bytes are HTML but the target isn't a web page, the "download" actually
        # failed — report it so the agent falls back to save_page (read visible text)
        # instead of saving junk that later parses to nothing.
        head = data[:1024].lstrip().lower()
        looks_html = head.startswith(b"<!doctype html") or head.startswith(b"<html")
        target_suffix = Path(safe_name).suffix.lower()
        if looks_html and target_suffix not in {".html", ".htm", ".txt", ""}:
            return {
                "ok": False,
                "message": "download_returned_html_not_file: 该链接返回的是网页而非文件，请改用 save_page 保存可见正文",
                "errorType": "download_html_mismatch",
                "bytes": len(data),
                "url": url,
            }
        target_path.write_bytes(data)
        return {
            "ok": True,
            "message": "downloaded",
            "path": str(target_path),
            "bytes": len(data),
            "url": url,
            "resourceName": resolved_resource_name,
            "dataDir": str(target_dir),
            "analysisDir": str(analysis_dir),
        }

    async def _save_playwright_download(
        self,
        download,
        source_url: str,
        resource_name: str | None = None,
    ) -> dict:
        suggested = download.suggested_filename or Path(urlparse(download.url or "").path).name or "downloaded_resource"
        safe_name = "".join(char for char in suggested if char.isalnum() or char in "._- ").strip() or "downloaded_resource"
        target_dir, analysis_dir, resolved_resource_name = self.resource_directories(
            source_url or download.url or self.page.url,
            safe_name,
            resource_name,
        )
        target_path = target_dir / safe_name
        await download.save_as(str(target_path))
        size = target_path.stat().st_size if target_path.exists() else 0
        return {
            "ok": bool(size > 0),
            "message": "downloaded" if size > 0 else "download_empty",
            "path": str(target_path) if size > 0 else "",
            "bytes": size,
            "url": download.url or source_url or self.page.url,
            "resourceName": resolved_resource_name,
            "dataDir": str(target_dir),
            "analysisDir": str(analysis_dir),
        }

    async def _try_download_dropdown(self, action: dict, source_url: str) -> dict | None:
        """Handle download controls whose first-level button only opens a menu.

        flk.npc.gov.cn uses an Element Plus hover dropdown: clicking the visible
        "下载" trigger does not download anything; the actual blob download is
        fired by the nested "点击下载" item.
        """
        target_id = action.get("target_id")
        if not target_id:
            return None
        try:
            handle = await self.page.evaluate_handle(
                "(targetId) => window.__pwAgentElements?.[targetId] || null",
                target_id,
            )
            element = handle.as_element()
            if element is None:
                return None
            text = (await element.inner_text()).strip()
            if text != "下载":
                return None
            await element.hover()
            await self.page.wait_for_timeout(300)
            candidates = self.page.get_by_text("点击下载", exact=True)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if not await candidate.is_visible():
                    continue
                async with self.page.expect_download(timeout=15000) as download_info:
                    await candidate.click()
                return await self._save_playwright_download(
                    await download_info.value,
                    source_url,
                    action.get("resource_name"),
                )
        except Exception:
            return None
        return None

    async def save_current_page(self, filename: str | None = None, resource_name: str | None = None) -> dict:
        page_data = await self.page.evaluate(SAVE_PAGE_SCRIPT)
        text = (page_data.get("text") or "").strip()
        if len(text) < 80:
            return {"ok": False, "message": "page_text_too_short", "errorType": "save_page_error"}
        title = page_data.get("title") or "saved_page"
        safe_name = filename or f"{title}.txt"
        safe_name = "".join(char for char in safe_name if char.isalnum() or char in "._- ").strip() or "saved_page.txt"
        if "." not in Path(safe_name).name:
            safe_name = f"{safe_name}.txt"
        target_dir, analysis_dir, resolved_resource_name = self.resource_directories(
            page_data.get("url") or self.page.url,
            safe_name,
            resource_name,
        )
        target_path = target_dir / safe_name
        content = "\n".join(
            [
                f"Title: {title}",
                f"Source URL: {page_data.get('url') or self.page.url}",
                "",
                text,
            ]
        )
        target_path.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "message": "saved_page",
            "path": str(target_path),
            "bytes": target_path.stat().st_size,
            "url": page_data.get("url") or self.page.url,
            "resourceName": resolved_resource_name,
            "dataDir": str(target_dir),
            "analysisDir": str(analysis_dir),
            "analysis": {
                "kind": "saved_page_text",
                "summary": f"Saved current page readable text, about {len(text)} characters.",
                "title": title,
            },
        }

    async def execute(self, action: dict) -> dict:
        await self.bring_page_to_front()
        name = action.get("action")
        if name == "download":
            return await self.download_url(action.get("url", ""), action.get("filename"), action.get("resource_name"))
        if name == "save_page":
            return await self.save_current_page(action.get("filename"), action.get("resource_name"))
        if name == "navigate":
            await self.page.goto(action["url"], wait_until="domcontentloaded", timeout=20000)
            await self.page.wait_for_timeout(700)
            await self.bring_page_to_front()
            return {"ok": True, "message": "navigating", "url": self.page.url, "pageId": self.page_id(self.page)}

        if name == "open_tab":
            page = await self.context.new_page()
            self.page = page
            self.context = page.context
            page_id = self.page_id(page)
            await self.bring_page_to_front()
            await page.goto(action["url"], wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(700)
            await self.bring_page_to_front()
            return {"ok": True, "message": "opened_tab", "url": page.url, "pageId": page_id}

        if name == "switch_tab":
            previous_page_id = self.page_id(self.page)
            page = self.resolve_page_target(action)
            if page is None:
                return {"ok": False, "message": "page_not_found", "errorType": "tab_error"}
            self.page = page
            self.context = page.context
            await self.bring_page_to_front()
            return {
                "ok": True,
                "message": "switched_tab",
                "url": page.url,
                "pageId": self.page_id(page),
                "previousPageId": previous_page_id,
            }

        if name == "back":
            await self.page.go_back(wait_until="domcontentloaded", timeout=10000)
            await self.page.wait_for_timeout(700)
            await self.bring_page_to_front()
            return {"ok": True, "message": "back"}

        known_pages = set(self.browser_pages())
        source_page_url = self.page.url
        if name == "click":
            dropdown_download = await self._try_download_dropdown(action, source_page_url)
            if dropdown_download is not None:
                return dropdown_download
        download_info = None
        expect_clicked_download = False
        if name == "click" and action.get("target_id"):
            try:
                expect_clicked_download = bool(
                    await self.page.evaluate(
                        """(targetId) => {
                          const el = window.__pwAgentElements?.[targetId];
                          if (!el) return false;
                          const link = el.closest?.("a[href]") || el;
                          const text = String(
                            el.innerText || el.textContent || el.getAttribute?.("aria-label") ||
                            el.getAttribute?.("title") || ""
                          ).toLowerCase();
                          const href = String(link.href || "").toLowerCase();
                          return Boolean(
                            link.hasAttribute?.("download") ||
                            /(下载|公报原版|wps版本|download)/i.test(text) ||
                            /\\.(pdf|docx?|xlsx?|zip|rar)(?:$|[?#])/.test(href)
                          );
                        }""",
                        action["target_id"],
                    )
                )
            except Exception:
                expect_clicked_download = False
        try:
            if expect_clicked_download:
                async with self.page.expect_download(timeout=1500) as download_info:
                    result = await self.page.evaluate(ACTION_SCRIPT, action)
            else:
                result = await self.page.evaluate(ACTION_SCRIPT, action)
        except Exception as exc:
            message = str(exc)
            if expect_clicked_download and "Timeout" in message and download_info is not None:
                result = {"ok": True, "message": "clicked"}
            else:
                download_info = None
            if name in {"click", "press"} and "Execution context was destroyed" in message:
                try:
                    await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                except Exception:
                    pass
                await self.page.wait_for_timeout(700)
                popup_timeout_ms = self.new_page_adoption_timeout_ms if self.page.url == source_page_url else 0
                adopted_page = await self.adopt_new_page_if_any(known_pages, popup_timeout_ms)
                await self.bring_page_to_front()
                return {
                    "ok": True,
                    "message": "action_triggered_navigation",
                    "url": self.page.url,
                    "adoptedNewPage": bool(adopted_page),
                }
            if download_info is None:
                raise
        if name in {"click", "press"}:
            if download_info is not None:
                try:
                    download = await download_info.value
                    return await self._save_playwright_download(
                        download,
                        source_page_url,
                        action.get("resource_name"),
                    )
                except Exception:
                    pass
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            await self.page.wait_for_timeout(700)
            may_open_link = name == "click" and bool(action.get("href"))
            popup_timeout_ms = (
                self.new_page_adoption_timeout_ms
                if may_open_link and self.page.url == source_page_url
                else 0
            )
            adopted_page = await self.adopt_new_page_if_any(known_pages, popup_timeout_ms)
            await self.bring_page_to_front()
            if adopted_page:
                return {
                    **result,
                    "message": "opened_new_page",
                    "url": self.page.url,
                    "adoptedNewPage": True,
                }
        return result

    async def close(self) -> None:
        if not self.connected_over_cdp:
            try:
                await self.browser.close()
            except Exception:
                pass
        try:
            await self._playwright.stop()
        except Exception:
            pass
        if self._cdp_process and self._close_cdp_process and self._cdp_process.poll() is None:
            try:
                self._cdp_process.terminate()
            except Exception:
                pass


def pretty_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
