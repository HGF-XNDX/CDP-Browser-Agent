"""Document-local observation; frame assembly lives in dom.py."""

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
    // planner read a long static document (e.g. a documentation page) in one shot
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
    if (tag === "select") return "use select_option with an observed value or label";
    if (type === "checkbox" || type === "radio") return "use set_checked with the desired boolean state";
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
      options: tag === "select" ? Array.from(el.options).slice(0, 100).map(o => ({
        value: o.value, label: o.label, selected: o.selected,
        disabled: o.disabled || Boolean(o.parentElement?.disabled)
      })) : undefined,
      multiple: tag === "select" ? el.multiple : undefined,
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
