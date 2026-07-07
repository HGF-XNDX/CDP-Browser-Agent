"""JSON extraction/repair utilities shared across the processing stack.

Robust against the chain-of-thought prose local thinking models (Qwen3 via
llama.cpp) emit around the actual JSON answer.
"""

from __future__ import annotations

import json
import re


def _repair_json_text(text: str) -> str:
    """Best-effort repair of common model JSON mistakes."""
    text = re.sub(
        r"(?<=[\{,\s])\s*'([^']*?)'\s*(?=:)", r'"\1"', text,
    )
    text = re.sub(
        r"(?<=:)\s*'([^']*?)'\s*(?=[,\}\]])", r'"\1"', text,
    )
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _strip_reasoning_prefix(text: str) -> str:
    """Remove chain-of-thought reasoning that local thinking models (Qwen3 via
    llama.cpp) prepend to the actual JSON answer.

    Handles three observed shapes:
      1. Paired <think>...</think> blocks.
      2. An *unclosed* <think> tag (open, no close) — strip from the tag to EOF
         only if JSON appears to follow; otherwise leave the text intact.
      3. Tagless reasoning prose ("Here's a thinking process...") before the
         JSON. We don't try to detect prose generically here — the balanced-brace
         scan in _iter_json_candidates handles that by locating the JSON itself.
    """
    # Paired tags first.
    text = re.sub(r"<think>[\s\S]*?</think>\s*", "", text)
    # Unclosed opening tag: drop everything up to and including it so the
    # downstream brace scan starts past the reasoning channel.
    if "<think>" in text and "</think>" not in text:
        text = text.split("<think>", 1)[1]
    # A bare closing tag with no opener (reasoning emitted before content begins).
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def _iter_json_candidates(text: str):
    """Yield balanced top-level {...} substrings in REVERSE order.

    Thinking models put the final answer last and often include example JSON
    inside the reasoning. A greedy `\\{[\\s\\S]*\\}` match spans from the first
    stray brace in the reasoning to the last brace of the answer, producing an
    unparseable blob. Scanning for individually balanced objects and trying the
    LAST one first reliably recovers the real answer.
    """
    starts: list[int] = []
    spans: list[tuple[int, int]] = []
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                starts.append(i)
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and starts:
                    spans.append((starts.pop(), i + 1))
    for start, end in reversed(spans):
        yield text[start:end]


def extract_json_object(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise json.JSONDecodeError(
            "Empty model response", "", 0,
        )
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    text = _strip_reasoning_prefix(text)
    # Fast path: the whole (post-strip) text is the JSON object.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # raw_decode at each top-level '{': tolerates stray/unbalanced braces in
    # reasoning prose (a lone '{' simply fails to decode and is skipped). We
    # advance a cursor past each decoded object so NESTED '{' aren't mistaken for
    # separate objects (which would return an inner fragment). Thinking models
    # emit the final answer LAST, so return the last decoded top-level dict.
    decoder = json.JSONDecoder()
    recovered: dict | None = None
    cursor = 0
    for m in re.finditer(r"\{", text):
        if m.start() < cursor:
            continue  # inside an already-decoded object → nested, skip
        try:
            obj, end = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        cursor = end
        if isinstance(obj, dict):
            recovered = obj
    if recovered is not None:
        return recovered
    # Scan balanced {...} candidates, last (= final answer) first, with repair.
    for candidate in _iter_json_candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        try:
            return json.loads(_repair_json_text(candidate))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(_repair_json_text(text))
    except json.JSONDecodeError:
        pass
    preview = text[:120]
    raise json.JSONDecodeError(
        "Could not extract valid JSON from model response "
        f"(length={len(text)}, preview={preview!r})",
        text,
        0,
    )

