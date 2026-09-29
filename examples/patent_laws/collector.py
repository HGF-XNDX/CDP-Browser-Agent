"""Deterministic official-source collection, exposed as an optional MCP workflow.

The planner chooses the workflow. Source parsing and JSON serialization never ask
the language model to reproduce statute text. Outputs are immutable and resumable.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
from typing import Any

from bs4 import BeautifulSoup
from mcp.server import MCPServer

from cdp_browser_agent.configuration import load_config
from cdp_browser_agent.processing.sessions import ProcessingSessions
from cdp_browser_agent.web.tools import WebTools
from cdp_browser_agent.workflows.store import atomic_json


SOURCES = {
    "CN": "https://www.cnipa.gov.cn/art/2020/11/23/art_97_155167.html",
    "US": "https://www.govinfo.gov/content/pkg/USCODE-2024-title35/html/USCODE-2024-title35.htm",
    "JP": "https://laws.e-gov.go.jp/api/2/law_data/334AC0000000121",
}


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def norm(value):
    return re.sub(r"\s+", " ", value).strip()


def compact(value):
    return re.sub(r"\s+", "", value)


def article(identity, number, heading, paragraphs, locator, *, scope="main", status="present", attributes=None):
    text = "\n".join(s for s in [heading, *paragraphs] if s)
    return {"article_id": identity, "number": str(number), "heading": heading,
        "scope": scope, "status": status, "paragraphs": paragraphs, "text": text,
        "text_sha256": sha(text), "source_locator": locator, "source_attributes": attributes or {}}


def chinese_number(value):
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    total, current = 0, 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in {"十", "百", "千"}:
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
        else:
            raise ValueError("Unknown Chinese article number")
    return total + current


def parse_cn(body):
    soup = BeautifulSoup(body, "html.parser")
    root = soup.select_one(".article-content")
    if root is None:
        raise ValueError("Official CN article container missing")
    articles, preamble, current, chapter = [], [], None, ""
    source_paragraphs = []
    for p in root.select("p"):
        text = norm(p.get_text())
        if not text:
            continue
        if re.match(r"^第[一二三四五六七八九十]+章", text):
            chapter = text
            continue
        found = re.match(r"^第([零一二三四五六七八九十百千]+)条\s*(.*)", text)
        if found:
            number = chinese_number(found[1])
            current = article(f"CN:main:{number}", number, f"第{found[1]}条", [],
                f".article-content p article-start={number}", attributes={"chapter": chapter})
            articles.append(current)
            current["paragraphs"].append(found[2])
            source_paragraphs.append(found[2])
        elif current:
            current["paragraphs"].append(text)
            source_paragraphs.append(text)
        else:
            preamble.append(text)
    if [a["number"] for a in articles] != [str(i) for i in range(1, 83)]:
        raise ValueError("CN source must contain exactly ordered articles 1..82; no partial export")
    for a in articles:
        a["text"] = "\n".join([a["heading"], *a["paragraphs"]])
        a["text_sha256"] = sha(a["text"])
    assert source_paragraphs == [p for a in articles for p in a["paragraphs"]]
    return {"title": "中华人民共和国专利法", "language": "zh-CN",
        "version": {"amended": "2020-10-17", "edition": "2020年修正", "source_published": "2020-11-23"},
        "articles": articles, "preamble": preamble, "supplementary_provisions": [], "annotations": [],
        "validation": {"source_article_count": 82, "mapped_article_count": len(articles),
            "source_paragraph_count": len(source_paragraphs), "ordered_number_coverage": True}}


def html_blocks(fragment):
    soup = BeautifulSoup(fragment, "html.parser")
    tags = {"p", "table", "h4", "h5", "h6"}
    blocks = [norm(t.get_text()) for t in soup.find_all(tags)
        if not any(p.name in tags for p in t.parents)]
    blocks = [x for x in blocks if x]
    if compact("".join(blocks)) != compact(soup.get_text()):
        raise ValueError("Unmapped text inside the official statutory body")
    return blocks


def parse_us(body):
    html = body.decode("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    heads = soup.select("h3.section-head")
    current = re.search(r"AUTHORITIES-LAWS-ENACTED-THROUGH-DATE:(\d{8})", html)
    edition = re.search(r"AUTHORITIES-PUBLICATION-NAME:([^<]+)", html)
    if not current or not edition or "TITLE 35" not in soup.get_text() or len(heads) < 100:
        raise ValueError("US official edition metadata or complete title is missing")
    articles, annotations, source_sections, blocks_count = [], [], [], 0
    # GovInfo's source document IDs separate statute sections from chapter TOCs
    # and historical notes, whose quoted section numbers must never create rows.
    for chunk in re.split(r"(?=<!-- documentid:)", html):
        piece = BeautifulSoup(chunk, "html.parser")
        heading_tag = piece.select_one("h3.section-head")
        if heading_tag is None:
            text = norm(piece.get_text())
            if text:
                annotations.append({"scope": "title_or_chapter", "text": text})
            continue
        heading = norm(heading_tag.get_text())
        identity = re.search(r"documentid:(\S+)", chunk)
        marker = identity[1] if identity else None
        number = re.match(r"\[?§{1,2}([0-9A-Za-z, ]+)\.", heading)
        if not marker or not number:
            raise ValueError("Unknown US section identifier")
        numbers = [n.strip() for n in number[1].split(",")]
        if not all(re.fullmatch(r"\d+[A-Za-z]?", n) for n in numbers):
            raise ValueError("Unrecognized grouped section; refusing to guess numbering")
        source_sections.append(marker)
        match = re.search(r"<!-- field-start:statute -->(.*?)<!-- field-end:statute -->", chunk, re.S)
        paragraphs = html_blocks(match[1]) if match else []
        status = "repealed" if "Repealed." in heading else "renumbered" if "Renumbered" in heading else "present"
        if not paragraphs and status == "present":
            raise ValueError(f"Missing body for active section {marker}")
        blocks_count += len(paragraphs)
        for n in numbers:
            articles.append(article(f"US:main:{n}", n, heading, paragraphs, f"documentid:{marker}",
                status=status, attributes={"shared_heading_numbers": numbers}))
        # Preserve editorial/historical notes separately, without calling them
        # operative statutory paragraphs. Raw HTML remains the exact source.
        tail = chunk[match.end():] if match else chunk.split(str(heading_tag), 1)[-1]
        note = norm(BeautifulSoup(tail, "html.parser").get_text())
        if note:
            annotations.append({"scope": "section", "source_locator": marker, "numbers": numbers, "text": note})
    if len(source_sections) != len(heads) or len(set(source_sections)) != len(heads):
        raise ValueError("US source heading coverage failed")
    # An independent count checks the official body paragraphs across the entire
    # document against paragraphs captured by the section boundary parser.
    official = [norm(p.get_text()) for p in soup.find_all("p")
        if any(c.startswith("statutory-body") for c in p.get("class", []))]
    exported = Counter(p for a in articles if len(a["source_attributes"]["shared_heading_numbers"]) == 1 for p in a["paragraphs"])
    if Counter(official) - exported:
        raise ValueError("US statutory paragraphs were lost at section boundaries")
    return {"title": "United States Code, Title 35 — Patents", "language": "en",
        "version": {"edition": edition[1].strip().removesuffix(" -->"),
            "laws_enacted_through": f"{current[1][:4]}-{current[1][4:6]}-{current[1][6:]}",
            "latest_available_law_verified": False,
            "scope_note": "Official 2024 edition; not represented as the latest 2026 consolidation."},
        "articles": articles, "supplementary_provisions": [], "annotations": annotations,
        "validation": {"source_heading_count": len(heads), "mapped_heading_count": len(source_sections),
            "mapped_article_count": len(articles), "grouped_number_expansion": len(articles)-len(heads),
            "source_statutory_paragraph_count": len(official), "statutory_paragraph_coverage": True,
            "mapped_block_count": blocks_count}}


def xml_text(element):
    return "".join(t.strip() for t in element.itertext()) if element is not None else ""


def xml_structure(element):
    """Retain table rows/cells and other non-article XML structure losslessly."""
    return {"tag": element.tag, "attributes": dict(element.attrib), "text": element.text or "",
        "tail": element.tail or "", "children": [xml_structure(c) for c in element]}


def parse_jp(body, asof):
    payload = json.loads(body)
    revision = payload["revision_info"]
    if payload["law_info"]["law_id"] != "334AC0000000121" or revision["amendment_enforcement_date"] > asof:
        raise ValueError("Wrong or future Japanese law revision")
    xml = base64.b64decode(payload["law_full_text"], validate=True)
    if len(xml) > 10_000_000 or b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
        raise ValueError("Unexpected or oversized law XML")
    root = ET.fromstring(xml)
    main = root.find("./LawBody/MainProvision")
    if root.tag != "Law" or main is None:
        raise ValueError("Official Law/MainProvision structure missing")
    articles, supplements = [], []
    groups = [("main", main)] + [(f"supplementary:{i}", s) for i, s in enumerate(root.findall("./LawBody/SupplProvision"), 1)]
    for group, container in groups:
        for node in container.iter("Article"):
            number = node.get("Num")
            title = xml_text(node.find("ArticleTitle"))
            caption = xml_text(node.find("ArticleCaption"))
            if not number or not title:
                raise ValueError("Japanese article number missing")
            paragraphs = [xml_text(p) for p in node.findall("Paragraph")]
            text = "\n".join(s for s in [caption, title, *paragraphs] if s)
            if compact(text) != compact(xml_text(node)):
                raise ValueError("Japanese article has unhandled structural content")
            a = article(f"JP:{group}:{number}", number, "\n".join(s for s in [caption, title] if s), paragraphs,
                f"{group}/Article[@Num='{number}']", scope="main" if group == "main" else "supplementary",
                status="deleted" if node.get("Delete") == "true" or "削除" == "".join(paragraphs) else "present",
                attributes={"article": dict(node.attrib), "group": dict(container.attrib),
                    "paragraph_numbers": [p.get("Num") for p in node.findall("Paragraph")]})
            articles.append(a)
        if group != "main":
            supplements.append({"group_id": group, "attributes": dict(container.attrib),
                "text": xml_text(container), "article_ids": [a["article_id"] for a in articles if a["article_id"].startswith(f"JP:{group}:")],
                "unnumbered_article_paragraphs": [xml_text(p) for p in container.findall("Paragraph")]})
    source_count = len(root.findall(".//Article"))
    if len(articles) != source_count or len(main.findall(".//Article")) < 190:
        raise ValueError("Japanese article coverage failed")
    ancillary = [el for el in root.find("LawBody") if el.tag not in {"LawTitle", "TOC", "MainProvision", "SupplProvision"}]
    appendices = [{"element": el.tag, "text": xml_text(el), "structure": xml_structure(el)} for el in ancillary]
    return {"title": xml_text(root.find("./LawBody/LawTitle")), "language": "ja",
        "version": {"as_of": asof, "law_info": payload["law_info"], "revision_info": revision,
            "scope_note": "Main provisions and the supplementary provisions present in this official XML. Extract=true marks abridged amendment supplements."},
        "articles": articles, "supplementary_provisions": supplements, "annotations": [], "appendices": appendices,
        "validation": {"source_article_count": source_count, "mapped_article_count": len(articles),
            "main_article_count": sum(a["scope"] == "main" for a in articles),
            "supplementary_article_count": sum(a["scope"] != "main" for a in articles),
            "supplementary_group_count": len(supplements), "xml_article_text_coverage": True,
            "source_ancillary_count": len(ancillary), "mapped_ancillary_count": len(appendices),
            "source_appendix_table_count": len(root.findall("./LawBody/AppdxTable")),
            "mapped_appendix_table_count": sum(a["element"] == "AppdxTable" for a in appendices)}}


class PatentCollection:
    def __init__(self, config, output, asof):
        date.fromisoformat(asof)
        self.asof = asof
        self.root = Path(output).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = deepcopy(config)
        self.config["learning"]["enabled"] = False
        self.config["processing"].update(paths=[str(Path(__file__).with_name("article-copy.json"))],
            artifact_dir=str(self.root / "workers"))
        self.lock = asyncio.Lock()

    def folder(self, country):
        if country not in SOURCES:
            raise ValueError("country must be CN, US or JP")
        return self.root / country

    def source(self, country):
        folder = self.folder(country)
        receipt = json.loads((folder / "source-result.json").read_text(encoding="utf-8"))
        if receipt["as_of"] != self.asof:
            raise ValueError("Collection date changed; use a new output directory")
        for filename, expected in receipt["files"].items():
            if sha((self.root / filename).read_bytes()) != expected:
                raise ValueError("Source evidence changed; use a new collection")
        return receipt

    async def fetch(self, country):
        async with self.lock:
            folder = self.folder(country)
            if (folder / "source-result.json").exists():
                r = self.source(country)
                return {"ok": True, "country": country, "status": "source_ready", "reused": True,
                    "source_url": r["url"], "response_sha256": r["response_sha256"]}
            folder.mkdir(parents=True, exist_ok=True)
            web = WebTools({**self.config["web"], "timeout_seconds": 60, "max_response_bytes": 10_000_000},
                artifact_root=folder / "source")
            url = SOURCES[country]
            if country == "JP":
                url += f"?asof={self.asof}&law_full_text_format=xml"
            result = await web.fetch(url)
            atomic_json(folder / "fetch-attempt.json", result)
            if not result["ok"]:
                return result
            raw = Path(result["artifact_paths"][1])
            if country == "JP":
                xml = base64.b64decode(json.loads(raw.read_bytes())["law_full_text"], validate=True)
                if len(xml) > 10_000_000:
                    raise ValueError("Oversized source XML")
                xml_path = raw.with_name("law.xml")
                xml_path.write_bytes(xml)
                result["artifact_paths"].append(str(xml_path))
            receipt = {k: result.get(k) for k in ("url", "requested_url", "accessed_at", "content_type", "response_sha256",
                "text_sha256", "network_attempts", "network_route", "total_chars")}
            receipt.update(as_of=self.asof, raw_path=raw.relative_to(self.root).as_posix(),
                files={Path(p).relative_to(self.root).as_posix(): sha(Path(p).read_bytes()) for p in result["artifact_paths"]})
            atomic_json(folder / "source-result.json", receipt)
            return {"ok": True, "country": country, "status": "source_ready", "source_url": url,
                "source_bytes": raw.stat().st_size, "response_sha256": receipt["response_sha256"],
                "next_step": "patent_articles_export(country); full source stays on disk, not in model context"}

    async def export(self, country):
        async with self.lock:
            folder = self.folder(country)
            receipt = self.source(country)
            body = (self.root / receipt["raw_path"]).read_bytes()
            law = {"CN": parse_cn, "US": parse_us, "JP": lambda b: parse_jp(b, self.asof)}[country](body)
            ids = [a["article_id"] for a in law["articles"]]
            if not ids or len(ids) != len(set(ids)):
                raise ValueError("Empty or duplicate article identities")
            law.update(schema_version=1, country=country, source=receipt,
                article_count=len(ids), content_policy="original language; whitespace normalized; no model rewriting")
            rows = [{"record_key": a["article_id"], "source_url": receipt["url"], "data": {"article": a}} for a in law["articles"]]
            workers = ProcessingSessions(self.config)
            reference = folder / "worker.json"
            if reference.exists():
                worker = json.loads(reference.read_text(encoding="utf-8"))
            else:
                worker = await workers.create("patent-article-copy", rows)
                atomic_json(reference, {"worker_session_id": worker["worker_session_id"]})
            outcome = await workers.run(worker["worker_session_id"])
            if not outcome["ok"]:
                return {"ok": False, "country": country, "status": "processing_failed", "worker": outcome}
            processed = json.loads(Path(outcome["records_path"]).read_text(encoding="utf-8"))
            exported = [r["data"]["article"] for r in processed]
            if exported != law["articles"]:
                raise ValueError("Worker output differs from the source-derived article objects")
            law["articles"] = exported
            law["validation"].update(unique_article_ids=True, worker_exact_copy=True,
                source_hash_verified=True, body_rewritten_by_model=False,
                semantic_legal_review=False, worker_session_id=outcome["worker_session_id"])
            destination = folder / "law.json"
            if destination.exists():
                old = json.loads(destination.read_text(encoding="utf-8"))
                if old != law:
                    raise ValueError("Committed law output differs; refusing to overwrite")
            else:
                atomic_json(destination, law)
            summary = {"ok": True, "country": country, "status": "validated", "article_count": len(ids),
                "main_article_count": sum(a["scope"] == "main" for a in exported),
                "source_url": receipt["url"], "version": law["version"],
                "output_path": str(destination), "output_sha256": sha(destination.read_bytes()),
                "worker_session_id": outcome["worker_session_id"], "processing_model_calls": outcome["model_calls"],
                "validation": law["validation"]}
            atomic_json(folder / "delivery.json", summary)
            return summary

    def status(self):
        rows = []
        for country in SOURCES:
            folder = self.folder(country)
            if (folder / "delivery.json").exists():
                r = json.loads((folder / "delivery.json").read_text(encoding="utf-8"))
                self.source(country)
                if sha((folder / "law.json").read_bytes()) != r["output_sha256"]:
                    raise ValueError("Committed output changed")
                rows.append(r)
            else:
                rows.append({"country": country, "status": "source_ready" if (folder / "source-result.json").exists() else "pending"})
        return {"ok": all(r["status"] == "validated" for r in rows), "countries": rows}


def create_server(collection):
    server = MCPServer("Patent law collection example")

    @server.tool()
    async def patent_source_fetch(country: str) -> dict[str, Any]:
        """Fetch the configured official patent law for CN, US or JP; preserve full source and hashes. US is the 2024 edition, not latest 2026 law. Then call patent_articles_export."""
        return await collection.fetch(country)

    @server.tool()
    async def patent_articles_export(country: str) -> dict[str, Any]:
        """After source_fetch, split the FULL saved HTML/XML by official article boundaries, run a deterministic processing worker, validate coverage and export law.json. No text need be copied into arguments."""
        return await collection.export(country)

    @server.tool()
    async def patent_collection_status() -> dict[str, Any]:
        """Verify saved source/output hashes and show CN/US/JP completion, counts and artifact paths."""
        return collection.status()

    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--config")
    args = parser.parse_args()
    create_server(PatentCollection(load_config(args.config), args.output, args.as_of)).run("stdio")


if __name__ == "__main__":
    main()
