from __future__ import annotations

import json
import re
from pathlib import Path

from ..common.json_utils import extract_json_object
from ..model_client import chat_completion

# Trailing context after 第N条/章/节: a separator (full/half space, tab, NBSP),
# punctuation (：:、，。（(【), or end of line. Relaxed from "must be whitespace" so
# formats like '第一条：…', '第一条（…', a bare '第一条' line, or markitdown output
# all parse — these were silently dropped, so a downloaded law never merged.
_NUM = r"[一二三四五六七八九十百千万零〇\d]"
_TAIL = r"(?:[　\t  ：:、，。（(【]|$)"
_CHAPTER_RE = re.compile(rf"^第{_NUM}+(?:章|编|部分){_TAIL}")
_SECTION_RE = re.compile(rf"^第{_NUM}+节{_TAIL}")
_ARTICLE_RE = re.compile(rf"^第{_NUM}+条{_TAIL}")
_FIRST_CHAPTER_RE = re.compile(rf"^第一(?:章|编|部分){_TAIL}")
# Leading markdown / list / quote markers to strip before matching (markitdown
# emits '**第一条**', '## 第一章', '- 第一条', '> …' etc.).
_MD_PREFIX_RE = re.compile(r"^[\s>#*`\-+|·•]+")
# Source-site noise to strip so a collected title matches FaLv.json's clean form,
# e.g. "中华人民共和国侵权责任法 - 维基文库，自由的图书馆" → "中华人民共和国侵权责任法".
_TITLE_NOISE_RE = re.compile(
    r"\s*[\-—–]\s*(?:维基文库|百度百科|[^\-—–]{0,20}?(?:网|图书馆|门户|百科|数据库)).*$"
    r"|[_｜|][^_｜|]{2,}$"
)
_TITLE_VERSION_RE = re.compile(r"\s*[（(]\s*\d{4}年[^）)]*[）)]\s*$")
# Wikisource edit links inside chapter/section headers and article bodies.
_EDIT_MARK_RE = re.compile(r"\[\s*编辑(?:\s*[|｜]\s*编辑源代码)?\s*\]")


def _clean_noise(text: str) -> str:
    return _EDIT_MARK_RE.sub("", text).strip()


def _clean_title(text: str) -> str:
    return _TITLE_VERSION_RE.sub("", _TITLE_NOISE_RE.sub("", text or "")).strip()


def _extract_title(lines: list[str]) -> str:
    for line in lines[:5]:
        stripped = line.strip()
        if stripped.startswith("Title:"):
            raw = _clean_noise(stripped[len("Title:"):].strip())
            return _clean_title(raw)
    for line in lines[:40]:
        stripped = _clean_noise(line.strip())
        # Standard law title: 中华人民共和国X法
        cleaned = _clean_title(stripped)
        if re.match(r"^中华人民共和国[一-鿿]{2,}法$", cleaned):
            return cleaned
        # Guidelines, regulations, measures, rules, etc.
        if re.match(r"^[一-鿿]{4,30}(?:指南|规定|条例|办法|规则|细则|通则|准则|规程|暂行规定|实施细则)$", stripped):
            return stripped
    return ""


def _norm_line(line: str) -> str:
    """Normalise a line for structure matching: strip leading markdown/list/quote
    markers and inline bold/code markers (markitdown emits '**第一条**', '## 第一章'),
    then strip source-site noise. So Office/HTML-derived law text parses too."""
    s = _MD_PREFIX_RE.sub("", line.strip())
    s = s.replace("*", "").replace("`", "")
    return _clean_noise(s)


def parse_law_txt(path: Path) -> dict | None:
    """Parse a downloaded Chinese law .txt file into a single FaLv.json entry."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    lines = [_norm_line(line) for line in text.splitlines()]
    title = _extract_title(text.splitlines())

    # Skip table of contents: find the 第一章 occurrence that is followed by
    # the most article lines — this handles both files where the TOC comes first
    # (prefer later position) and files where a short repeat section appears at
    # the end (prefer earlier position with more content).
    first_chapter_positions = [i for i, line in enumerate(lines) if _FIRST_CHAPTER_RE.match(line)]
    if not first_chapter_positions:
        first_chapter_positions = [0]
    start_idx = max(first_chapter_positions, key=lambda pos: sum(1 for line in lines[pos:] if _ARTICLE_RE.match(line)))

    chapters: list[dict] = []
    current_chapter: dict | None = None
    pending: list[str] = []

    def flush() -> None:
        nonlocal pending
        if pending and current_chapter is not None:
            current_chapter["articles"].append(" ".join(pending))
        pending = []

    for stripped in lines[start_idx:]:
        if not stripped:
            continue
        if _CHAPTER_RE.match(stripped) or _SECTION_RE.match(stripped):
            flush()
            current_chapter = {"chapter_title": stripped, "articles": []}
            chapters.append(current_chapter)
        elif _ARTICLE_RE.match(stripped):
            flush()
            if current_chapter is None:
                current_chapter = {"chapter_title": "", "articles": []}
                chapters.append(current_chapter)
            pending = [stripped]
        elif pending:
            pending.append(stripped)

    flush()

    non_empty = [ch for ch in chapters if ch["articles"]]
    if not non_empty:
        return None

    return {
        "title": title or path.stem,
        "name": "法律",
        "content": non_empty,
    }


def law_dedup_key(title: str) -> str:
    """Normalised identity of a law for de-duplication: drop 国名前缀, all bracket
    variants, leading institution/category labels, and trailing version/source noise
    so '中华人民共和国民事诉讼法', '中华人民共和国民事诉讼法（2023年修正）' and
    '国家知识产权局 法律 中华人民共和国专利法(2020年修正)' collapse to one key."""
    s = re.sub(r"[\s《》〈〉﹤﹥＜＞<>（）()【】]", "", title or "")
    s = s.replace("中华人民共和国", "").replace("中国人民共和国", "")
    s = re.sub(r"^(?:国家知识产权局|国务院|最高人民法院|全国人民代表大会(?:常务委员会)?|法律|行政法规|司法解释)+", "", s)
    s = re.split(r"[-—_]|\d{4}年", s)[0]
    return s.strip()


def _article_count(entry: dict) -> int:
    return sum(len(c.get("articles") or []) for c in entry.get("content", []) if isinstance(c, dict))


def merge_into_law_db(entry: dict, db_path: Path) -> bool:
    """Merge a parsed law entry into a FaLv.json-style database, de-duplicated by
    law identity (not exact title). If the same law already exists, KEEP the more
    complete version (more articles) — so a collected full text REPLACES a partial
    excerpt instead of creating a duplicate. Returns True if the file was changed."""
    try:
        existing: list[dict] = json.loads(db_path.read_text(encoding="utf-8")) if db_path.exists() else []
        if not isinstance(existing, list):
            return False
        key = law_dedup_key(entry.get("title", ""))
        n_new = _article_count(entry)
        for i, e in enumerate(existing):
            if isinstance(e, dict) and law_dedup_key(e.get("title", "")) == key:
                if n_new > _article_count(e):
                    existing[i] = entry  # collected full text supersedes the excerpt
                    db_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                    return True
                return False  # existing is as/more complete — keep it, no duplicate
        existing.append(entry)
        db_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def normalize_law_txt(txt_path: Path) -> Path | None:
    """Parse a law .txt file and write a structured JSON file alongside it.

    Returns the JSON path on success, None if the file could not be parsed into
    a law structure (in which case the original .txt should be kept as-is).
    """
    parsed = parse_law_txt(txt_path)
    if not parsed:
        return None
    out_path = txt_path.with_name(txt_path.stem + "_normalized.json")
    out_path.write_text(json.dumps([parsed], ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


_LOOSE_ARTICLE_RE = re.compile(rf"第{_NUM}+条")


async def llm_normalize_law_txt(txt_path: Path, config: dict) -> Path | None:
    """LLM FALLBACK when the deterministic parser fails on an odd format: hand the
    downloaded law text to the model to split into the FaLv structure. Used only
    when `parse_law_txt` returns nothing but the text clearly contains law articles.
    The model is told to copy articles VERBATIM (no rewrite/omit) so grounding stays
    valid. Returns the normalized.json path, or None."""
    try:
        text = txt_path.read_text(encoding="utf-8")
    except Exception:
        return None
    if len(set(_LOOSE_ARTICLE_RE.findall(text))) < 2:
        return None  # not law text — don't waste an LLM call
    io_instruction = str(
        ((config.get("agent") or {}).get("data_io_agent_instruction") or "")
    ).strip()
    messages = [
        {
            "role": "system",
            "content": (
                "你是法律文本结构化工具。把给定的法律/法规/条文正文逐条切分为 JSON："
                '{"title":"法律名称","name":"法律","content":[{"chapter_title":"章节标题(无则空串)",'
                '"articles":["第N条 完整逐字原文", ...]}]}。\n'
                "要求：① 每个 article 以『第N条』开头，必须是**完整逐字原文**，"
                "绝不改写、概括、省略或补全；② 按原文章节分组，没有章节就用一个 chapter_title 为空串的块；"
                "③ 去除网页导航/页眉页脚/编辑标记等非正文内容；④ 只输出 JSON，不要解释。"
                + (
                    f"\n【规划智能体给数据读写智能体的本次职责】\n{io_instruction}"
                    if io_instruction
                    else ""
                )
            ),
        },
        {"role": "user", "content": text[:60000]},
    ]
    try:
        out = await chat_completion(
            messages, {**(config.get("model") or {}), "enableThinking": False, "maxTokens": 16000}
        )
        entry = extract_json_object(out)
    except Exception:
        return None
    if not isinstance(entry, dict) or _article_count(entry) < 2:
        return None
    if not str(entry.get("title") or "").strip():
        entry["title"] = _extract_title(text.splitlines()) or txt_path.stem
    entry.setdefault("name", "法律")
    out_path = txt_path.with_name(txt_path.stem + "_normalized.json")
    out_path.write_text(json.dumps([entry], ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path
