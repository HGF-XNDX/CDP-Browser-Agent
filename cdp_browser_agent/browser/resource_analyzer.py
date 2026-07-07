# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import gzip
import json
import os
import re
import tarfile
import zipfile
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import urlparse

from ..model_client import chat_completion


DEFAULT_DOWNLOAD_DIR = Path(os.getenv("CDPAGENT_TEST_DOWNLOAD_DIR", "downloads")).resolve()
DEFAULT_REPORT_JSON = "resource_analysis.json"
DEFAULT_REPORT_MD = "resource_analysis.md"
DOWNLOAD_MANIFEST = "resource_download_manifest.json"
SUPPORTED_DATA_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl"}
OBSERVED_RESOURCE_SUFFIXES = (".csv", ".tsv", ".json", ".jsonl", ".parquet", ".zip", ".tar", ".tar.gz", ".tgz", ".gz")


def load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.is_absolute():
        package_path = Path(__file__).resolve().parent / config_path
        cwd_path = Path.cwd() / config_path
        config_path = package_path if package_path.exists() else cwd_path
    return json.loads(config_path.read_text(encoding="utf-8"))


def configure_for_download(base_config: dict, download_dir: Path) -> dict:
    config = json.loads(json.dumps(base_config, ensure_ascii=False))
    config.setdefault("browser", {})["downloads_path"] = str(download_dir)
    agent_cfg = config.setdefault("agent", {})
    agent_cfg["task_sequence"] = {
        "index": 1,
        "total": 2,
        "task_key": "find_download_resource",
    }
    # Law collection: a saved page must actually contain 法条正文 (多条 第N条) to
    # count as success — otherwise the agent converges on a metadata/landing page
    # (e.g. flk.npc.gov.cn detail shell) and merges nothing usable.
    agent_cfg["require_law_articles"] = True
    return config


def detect_resource_type(path: Path) -> str:
    try:
        if zipfile.is_zipfile(path):
            return "zip"
        if tarfile.is_tarfile(path):
            return "tar"
        with path.open("rb") as file:
            magic = file.read(4)
        if magic.startswith(b"\x1f\x8b"):
            return "gzip"
    except OSError:
        return "unknown"
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_DATA_SUFFIXES:
        return suffix.lstrip(".")
    return "unknown"


def is_safe_child(parent: Path, child: Path) -> bool:
    parent_resolved = parent.resolve()
    child_resolved = child.resolve()
    return child_resolved == parent_resolved or parent_resolved in child_resolved.parents


def safe_extract_zip(path: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            target = destination / member.filename
            if not is_safe_child(destination, target):
                raise ValueError(f"Unsafe ZIP member path: {member.filename}")
            archive.extract(member, destination)
            if not member.is_dir():
                extracted.append(target)
    return extracted


def safe_extract_tar(path: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            target = destination / member.name
            if not is_safe_child(destination, target):
                raise ValueError(f"Unsafe TAR member path: {member.name}")
            archive.extract(member, destination)
            if member.isfile():
                extracted.append(target)
    return extracted


def decompress_gzip(path: Path, destination: Path) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    output_name = path.name[:-3] if path.name.lower().endswith(".gz") else f"{path.stem}.decompressed"
    output_path = destination / output_name
    with gzip.open(path, "rb") as source:
        output_path.write_bytes(source.read())
    return [output_path]


def latest_downloaded_file(download_dir: Path) -> Path | None:
    excluded = {DEFAULT_REPORT_JSON, DEFAULT_REPORT_MD, DOWNLOAD_MANIFEST}
    candidates = [
        path
        for path in download_dir.iterdir()
        if path.is_file() and path.name not in excluded and not path.name.startswith(".")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def candidate_filename_from_url(url: str) -> str:
    path = urlparse(url or "").path.lower()
    if not any(path.endswith(suffix) for suffix in OBSERVED_RESOURCE_SUFFIXES):
        return ""
    return Path(urlparse(url).path).name


def collect_observed_candidate_resources(existing: list[dict], observation: dict) -> list[dict]:
    indexed = {str(item.get("filename") or "").lower(): dict(item) for item in existing if item.get("filename")}
    source_page = observation.get("url", "")
    for element in observation.get("elements") or []:
        href = str(element.get("href") or "")
        filename = candidate_filename_from_url(href)
        if not filename:
            continue
        key = filename.lower()
        candidate = {
            "filename": filename,
            "url": href,
            "source_page": source_page,
            "evidence": " ".join(
                str(element.get(field) or "")
                for field in ("text", "description", "nearbyText", "containerText")
            )[:300],
        }
        previous = indexed.get(key)
        if not previous or "/resolve/" in href or "download=true" in href:
            indexed[key] = candidate
    return sorted(indexed.values(), key=lambda item: str(item.get("filename") or "").lower())


def write_download_manifest(download_dir: Path, state: dict, analysis_dir: Path | None = None) -> None:
    analysis_dir = analysis_dir or download_dir
    analysis_dir.mkdir(parents=True, exist_ok=True)
    downloads = []
    for item in state.get("history") or []:
        action = item.get("action") or {}
        result = item.get("result") or {}
        if action.get("action") not in {"download", "save_page"} and result.get("message") not in {"downloaded", "saved_page"}:
            continue
        if not result.get("ok"):
            continue
        path = Path(result.get("path") or "")
        if not result.get("path") or not path.is_file() or path.stat().st_size <= 0:
            continue
        downloads.append(
            {
                "filename": action.get("filename") or path.name,
                "path": str(path),
                "source_url": result.get("url") or action.get("url", ""),
                "bytes": path.stat().st_size,
                "step": item.get("step"),
            }
        )
    if downloads:
        observed_candidates = [dict(item) for item in (state.get("observed_resource_candidates") or [])]
        downloaded_names = {str(item.get("filename") or "").lower() for item in downloads}
        for candidate in observed_candidates:
            candidate["downloaded"] = str(candidate.get("filename") or "").lower() in downloaded_names
        (analysis_dir / DOWNLOAD_MANIFEST).write_text(
            json.dumps(
                {"downloads": downloads, "observed_candidate_resources": observed_candidates},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def load_download_manifest(download_dir: Path, analysis_dir: Path | None = None) -> dict:
    path = (analysis_dir or download_dir) / DOWNLOAD_MANIFEST
    if not path.exists():
        return {"downloads": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"downloads": []}


def source_url_for_file(download_dir: Path, path: Path, analysis_dir: Path | None = None) -> str:
    manifest = load_download_manifest(download_dir, analysis_dir)
    path_resolved = str(path.resolve()).lower()
    for item in manifest.get("downloads") or []:
        item_path = str(Path(item.get("path") or "").resolve()).lower() if item.get("path") else ""
        if item_path == path_resolved or item.get("filename") == path.name:
            return item.get("source_url") or ""
    return ""


def manifest_downloaded_files(download_dir: Path, analysis_dir: Path | None = None) -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    for item in load_download_manifest(download_dir, analysis_dir).get("downloads") or []:
        path_text = item.get("path") or ""
        if not path_text:
            continue
        resolved = Path(path_text).resolve()
        key = str(resolved).lower()
        if key in seen or not resolved.is_file() or resolved.stat().st_size <= 0:
            continue
        seen.add(key)
        files.append(resolved)
    return files


def observed_candidate_completeness_warnings(download_dir: Path, analysis_dir: Path | None = None) -> tuple[list[str], dict]:
    manifest = load_download_manifest(download_dir, analysis_dir)
    candidates = manifest.get("observed_candidate_resources") or []
    downloads = manifest.get("downloads") or []
    candidate_names = sorted({str(item.get("filename") or "") for item in candidates if item.get("filename")})
    downloaded_names = {str(item.get("filename") or "") for item in downloads if item.get("filename")}
    acquired = sorted(set(candidate_names) & downloaded_names)
    missing = sorted(set(candidate_names) - downloaded_names)
    evidence = {
        "observed_candidate_file_count": len(candidate_names),
        "acquired_observed_candidate_count": len(acquired),
        "observed_candidate_files": candidate_names,
        "acquired_observed_candidate_files": acquired,
        "unacquired_observed_candidate_files": missing,
    }
    if not missing:
        return [], evidence
    examples = ", ".join(f"`{name}`" for name in missing[:8])
    return [
        (
            f"已观察到来源页面存在 {len(candidate_names)} 个同级资源文件，但本地仅下载其中 {len(acquired)} 个。"
            f"未获取的可见候选包括 {examples}。除非来源明确将当前文件定义为完整子集，否则本地产物只能视为选取样本。"
        )
    ], evidence


def supported_data_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED_DATA_SUFFIXES else []
    return sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_DATA_SUFFIXES)


def shard_sibling_data_files(path: Path) -> list[Path]:
    match = re.match(r"^(.+?[_-])(\d+)(\.[^.]+)$", path.name)
    if not match:
        return []
    prefix, _, suffix = match.groups()
    siblings = []
    for sibling in path.parent.glob(f"{prefix}*{suffix}"):
        sibling_match = re.match(rf"^{re.escape(prefix)}(\d+){re.escape(suffix)}$", sibling.name)
        if sibling_match and sibling.suffix.lower() in SUPPORTED_DATA_SUFFIXES:
            siblings.append((int(sibling_match.group(1)), sibling))
    return [sibling for _, sibling in sorted(siblings)]


def prepare_for_analysis(path: Path, download_dir: Path) -> tuple[str, list[Path], list[Path]]:
    resource_type = detect_resource_type(path)
    extract_root = download_dir / "extracted" / path.stem
    if resource_type == "zip":
        extracted = safe_extract_zip(path, extract_root)
        return resource_type, extracted, supported_data_files(extract_root)
    if resource_type == "tar":
        extracted = safe_extract_tar(path, extract_root)
        return resource_type, extracted, supported_data_files(extract_root)
    if resource_type == "gzip":
        extracted = decompress_gzip(path, extract_root)
        data_files: list[Path] = []
        for item in extracted:
            data_files.extend(supported_data_files(item))
        return resource_type, extracted, data_files
    data_files = shard_sibling_data_files(path) or supported_data_files(path)
    return resource_type, [path], data_files


def shard_completeness_warnings(target: Path, data_files: list[Path], analyzed_files: list[dict]) -> list[str]:
    warnings: list[str] = []
    candidate_paths = [target, *data_files]
    for path in candidate_paths:
        match = re.match(r"^(.+?[_-])(\d+)(\.[^.]+)$", path.name)
        if not match:
            continue
        prefix, index_text, suffix = match.groups()
        siblings = sorted(path.parent.glob(f"{prefix}*{suffix}"))
        sibling_numbers = []
        for sibling in siblings:
            sibling_match = re.match(rf"^{re.escape(prefix)}(\d+){re.escape(suffix)}$", sibling.name)
            if sibling_match:
                sibling_numbers.append(int(sibling_match.group(1)))
        index = int(index_text)
        if len(sibling_numbers) <= 1:
            warnings.append(
                f"`{path.name}` looks like one shard of a split dataset, but no sibling shards were found locally. "
                "This download should not be treated as a complete dataset unless the source page explicitly says it is a complete subset."
            )
        elif sorted(sibling_numbers) != list(range(min(sibling_numbers), max(sibling_numbers) + 1)):
            warnings.append(
                f"`{path.name}` belongs to shard group `{prefix}*{suffix}`, but local shard numbers are incomplete: {sorted(sibling_numbers)}."
            )
        elif index == max(sibling_numbers) and len(sibling_numbers) < 3:
            warnings.append(
                f"`{path.name}` is part of shard group `{prefix}*{suffix}` with only {len(sibling_numbers)} local shards; verify source completeness."
            )

    if len(analyzed_files) == 1:
        item = analyzed_files[0]
        columns = {str(column).lower() for column in item.get("columns") or []}
        if {"website", "domain", "subdomain", "annotation_id", "actions"}.issubset(columns) and item.get("row_count", 0) < 50:
            warnings.append(
                "The analyzed file has Mind2Web-like fields but very few top-level rows. It is likely a small shard/subset, not the full dataset."
            )
    return sorted(set(warnings))


def read_delimited(path: Path, delimiter: str = ",") -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, delimiter=delimiter)
        return list(reader.fieldnames or []), list(reader)


def read_json(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(value, list):
        rows = [item for item in value if isinstance(item, dict)]
    elif isinstance(value, dict):
        list_value = next((item for item in value.values() if isinstance(item, list)), None)
        rows = [item for item in list_value if isinstance(item, dict)] if isinstance(list_value, list) else [value]
    else:
        rows = []
    fields = sorted({field for row in rows for field in row})
    return fields, rows


def read_jsonl(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    fields = sorted({field for row in rows for field in row})
    return fields, rows


def numeric_stats(rows: list[dict[str, str]], fields: list[str]) -> dict[str, dict[str, float | int]]:
    stats: dict[str, dict[str, float | int]] = {}
    for field in fields:
        values: list[float] = []
        for row in rows:
            try:
                values.append(float(row.get(field, "")))
            except (TypeError, ValueError):
                continue
        if values and len(values) >= max(3, len(rows) // 2):
            stats[field] = {
                "count": len(values),
                "min": min(values),
                "max": max(values),
                "mean": round(mean(values), 4),
            }
    return stats


def categorical_counts(rows: list[dict[str, str]], fields: list[str]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for field in fields:
        values = [row.get(field, "").strip() for row in rows if row.get(field, "").strip()]
        if not values:
            continue
        if max(len(value) for value in values) > 500 or mean(len(value) for value in values) > 160:
            continue
        unique_values = set(values)
        if 1 < len(unique_values) <= 30:
            counts[field] = dict(Counter(values).most_common())
    return counts


def compact_value(value: Any, max_text: int = 500, depth: int = 0) -> Any:
    if depth >= 3:
        return "<nested>"
    if isinstance(value, str):
        return value if len(value) <= max_text else value[:max_text] + f"... <truncated {len(value) - max_text} chars>"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        items = [compact_value(item, max_text=max_text, depth=depth + 1) for item in value[:3]]
        if len(value) > 3:
            items.append(f"... <{len(value) - 3} more items>")
        return items
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 12:
                result["..."] = f"<{len(value) - 12} more keys>"
                break
            lowered = str(key).lower()
            field_limit = 180 if lowered in {"raw_html", "cleaned_html", "html", "dom", "screenshot"} else max_text
            result[str(key)] = compact_value(item, max_text=field_limit, depth=depth + 1)
        return result
    return compact_value(str(value), max_text=max_text, depth=depth)


def compact_rows(rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    return [compact_value(row) for row in rows[:limit]]


def analyze_data_file(path: Path) -> dict | None:
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            fields, rows = read_delimited(path)
        elif suffix == ".tsv":
            fields, rows = read_delimited(path, "\t")
        elif suffix == ".json":
            fields, rows = read_json(path)
        elif suffix == ".jsonl":
            fields, rows = read_jsonl(path)
        else:
            return None
    except Exception as exc:
        return {"file": str(path), "error": str(exc), "row_count": 0, "columns": []}

    string_rows = [{key: "" if value is None else str(value) for key, value in row.items()} for row in rows]
    return {
        "file": str(path),
        "file_type": suffix.lstrip("."),
        "row_count": len(rows),
        "columns": fields,
        "numeric_stats": numeric_stats(string_rows, fields),
        "categorical_counts": categorical_counts(string_rows, fields),
        "sample_rows": compact_rows(rows[:5]),
    }


def infer_dataset_purpose(report: dict) -> str:
    text = json.dumps(report, ensure_ascii=False).lower()
    columns = {
        str(column).lower()
        for item in report.get("analyzed_files") or []
        for column in (item.get("columns") or [])
    }
    if {"website", "domain", "subdomain", "annotation_id", "actions"}.issubset(columns):
        return "网页智能体任务数据集，用于训练或评估智能体在真实网站上的任务理解、页面观察和动作规划能力。"
    if {"starting url", "category", "task"}.issubset(columns) or {"starting_url", "category", "task"}.issubset(columns):
        return "浏览器智能体 benchmark 任务集，用于评估智能体根据自然语言任务在指定网站中查找信息或完成操作的能力。"
    if "webarena" in text:
        return "网页环境智能体 benchmark 相关数据，用于评估智能体在模拟网站或真实网站中的导航和任务完成能力。"
    if "mind2web" in text:
        return "网页智能体交互轨迹数据，用于研究跨网站任务执行、动作预测和网页操作泛化。"
    if any(name in text for name in ["osworld", "gui", "desktop"]):
        return "GUI 或电脑使用智能体 benchmark 数据，用于评估模型操作软件界面和完成桌面任务的能力。"
    return "公开结构化数据资源，可用于后续做数据结构理解、字段分析、样例检查和轻量统计。"


def infer_source_info(report: dict, download_dir: Path, analysis_dir: Path | None = None) -> dict:
    source = report.get("downloaded_resource", "")
    original_url = source_url_for_file(download_dir, Path(source), analysis_dir) if source else ""
    resource_paths = [Path(value) for value in report.get("downloaded_resources") or ([source] if source else [])]
    source_urls = [source_url_for_file(download_dir, path, analysis_dir) for path in resource_paths]
    source_urls = [url for url in source_urls if url]
    lower = (original_url or source).lower()
    source_type = "local_file"
    if "huggingface.co" in lower:
        source_type = "huggingface"
    elif "github.com" in lower or "raw.githubusercontent.com" in lower:
        source_type = "github"
    elif lower.startswith(("http://", "https://")):
        source_type = "web_url"
    return {
        "downloaded_resource": source,
        "downloaded_resources": [str(path) for path in resource_paths],
        "source_url": original_url,
        "source_urls": source_urls,
        "source_type": source_type,
        "resource_type": report.get("resource_type", ""),
    }


def build_dataset_summary(report: dict, download_dir: Path, analysis_dir: Path | None = None) -> dict:
    analyzed_files = report.get("analyzed_files") or []
    total_rows = sum(int(item.get("row_count") or 0) for item in analyzed_files)
    file_summaries = []
    all_columns = []
    for item in analyzed_files:
        columns = item.get("columns") or []
        all_columns.extend(columns)
        file_summaries.append(
            {
                "file": item.get("file", ""),
                "file_type": item.get("file_type", ""),
                "row_count": item.get("row_count", 0),
                "columns": columns,
                "sample_row": (item.get("sample_rows") or [{}])[0] if item.get("sample_rows") else {},
            }
        )
    unique_columns = list(dict.fromkeys(all_columns))
    completeness_warnings = report.get("completeness_warnings") or []
    name_hint = download_dir.parent.name if download_dir.name.lower() == "data" else Path(report.get("downloaded_resource") or "").stem
    return {
        "name_hint": name_hint,
        "source": infer_source_info(report, download_dir, analysis_dir),
        "purpose": infer_dataset_purpose(report),
        "structure": {
            "downloaded_type": report.get("resource_type", ""),
            "extracted_file_count": report.get("extracted_file_count", 0),
            "data_file_count": report.get("data_file_count", 0),
            "analyzed_file_count": len(analyzed_files),
            "total_rows_in_analyzed_files": total_rows,
            "columns_observed": unique_columns[:80],
        },
        "files": file_summaries[:20],
        "completeness": {
            "warnings": completeness_warnings,
            "local_files_readable": bool(analyzed_files),
            "dataset_coverage_verified": False,
            "status": "warning" if completeness_warnings else "unverified",
            "looks_complete": False if completeness_warnings else None,
        },
        "recommended_use": (
            "适合做浏览器/网页/GUI 智能体的数据理解、任务类型分布分析、样例任务检查和后续评测集构建。"
            if any(keyword in infer_dataset_purpose(report) for keyword in ["智能体", "benchmark"])
            else "适合做轻量数据结构探索和字段统计。"
        ),
    }


def authoritative_summary_text(report: dict) -> str:
    summary = report.get("dataset_summary") or {}
    source = summary.get("source") or {}
    structure = summary.get("structure") or {}
    completeness = summary.get("completeness") or {}
    files = summary.get("files") or []
    local_resources = source.get("downloaded_resources") or [source.get("downloaded_resource") or report.get("downloaded_resource", "")]
    resource_names = ", ".join(f"`{Path(resource).name}`" for resource in local_resources if resource)
    source_urls = source.get("source_urls") or ([source.get("source_url")] if source.get("source_url") else [])
    completeness_text = (
        "本地产物存在需要确认的问题，不能视为完整数据集"
        if completeness.get("warnings")
        else "本地已下载文件可解析，但仅凭本地产物无法确认其覆盖源数据集的全部必要文件"
    )
    lines = [
        f"该摘要以本地真实下载并解析的文件为准，不使用网页 README 或智能体浏览阶段的估算数字。",
        f"本地数据资源为 {resource_names or '`unknown`'}，来源 URL 为 {', '.join(source_urls) or 'unknown'}。",
        f"本地检测类型为 `{source.get('resource_type') or report.get('resource_type')}`，共发现 {structure.get('data_file_count', 0)} 个数据文件，实际分析 {structure.get('analyzed_file_count', 0)} 个文件。",
        f"实际分析总行数为 {structure.get('total_rows_in_analyzed_files', 0)}，字段为：{', '.join(structure.get('columns_observed') or [])}。",
        f"用途判断：{summary.get('purpose', '')}",
        f"完整性判断：{completeness_text}。",
    ]
    if completeness.get("warnings"):
        lines.append("完整性警告：" + "；".join(completeness.get("warnings") or []))
    if files:
        first = files[0]
        lines.append(
            f"主要文件 `{Path(first.get('file', '')).name}` 包含 {first.get('row_count', 0)} 行，"
            f"{len(first.get('columns') or [])} 个字段。"
        )
    return "\n".join(line for line in lines if line)


async def model_summarize(config: dict, report: dict) -> str:
    model_settings = {**(config.get("model") or {}), "maxTokens": 900, "temperature": 0.1, "enableThinking": False}
    summary_payload = {
        "authoritative_rule": "Only use values in this payload. Local parsed file data is authoritative. Do not use README/browser claims or infer larger project totals.",
        "dataset_summary": report.get("dataset_summary", {}),
        "analyzed_files": [
            {
                "file": item.get("file", ""),
                "file_type": item.get("file_type", ""),
                "row_count": item.get("row_count", 0),
                "columns": item.get("columns", []),
                "numeric_stats": item.get("numeric_stats", {}),
                "categorical_counts": item.get("categorical_counts", {}),
                "sample_rows": item.get("sample_rows", [])[:2],
            }
            for item in report.get("analyzed_files", [])
        ],
        "completeness_warnings": report.get("completeness_warnings", []),
    }
    raw = await chat_completion(
        [
            {
                "role": "system",
                "content": (
                    "You write a dataset information summary for a browser-agent test pipeline. "
                    "The local parsed file data provided by the user is authoritative. "
                    "Do not use browser-stage claims, README project totals, or external knowledge. "
                    "Every row count, column name, file count, source URL, and completeness claim must match the provided JSON exactly. "
                    "Do not describe the dataset download as complete when dataset_coverage_verified is false; describe only the analyzed local artifact and unresolved coverage. "
                    "If the source page and local file conflict, local parsed file data wins. "
                    "Output JSON only: "
                    "{\"dataset_overview\":\"...\",\"source\":\"...\",\"structure\":\"...\","
                    "\"intended_use\":\"...\",\"completeness\":\"...\",\"next_steps\":\"...\"}."
                ),
            },
            {"role": "user", "content": json.dumps(summary_payload, ensure_ascii=False)},
        ],
        model_settings,
    )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    return "\n".join(
        part
        for part in [
            parsed.get("dataset_overview", ""),
            f"Source: {parsed.get('source')}" if parsed.get("source") else "",
            f"Structure: {parsed.get('structure')}" if parsed.get("structure") else "",
            f"Intended use: {parsed.get('intended_use')}" if parsed.get("intended_use") else "",
            f"Completeness: {parsed.get('completeness')}" if parsed.get("completeness") else "",
            f"Next steps: {parsed.get('next_steps')}" if parsed.get("next_steps") else "",
        ]
        if part
    )


def write_markdown(path: Path, report: dict, model_summary: str = "") -> None:
    summary = report.get("dataset_summary") or {}
    structure = summary.get("structure") or {}
    source = summary.get("source") or {}
    completeness = summary.get("completeness") or {}
    lines = [
        "# Dataset Information Summary",
        "",
        "## Verified Local Summary",
        "",
        authoritative_summary_text(report),
        "",
        "## Overview",
        "",
        f"- Name hint: `{summary.get('name_hint', '')}`",
        f"- Purpose: {summary.get('purpose', '')}",
        f"- Recommended use: {summary.get('recommended_use', '')}",
        "",
        "## Source",
        "",
        f"- Downloaded resources: {', '.join(f'`{item}`' for item in (source.get('downloaded_resources') or [source.get('downloaded_resource') or report['downloaded_resource']]))}",
        f"- Source URLs: {', '.join(source.get('source_urls') or ([source.get('source_url')] if source.get('source_url') else ['unknown']))}",
        f"- Source type: `{source.get('source_type', '')}`",
        f"- Detected resource type: `{source.get('resource_type') or report['resource_type']}`",
        "",
        "## Structure",
        "",
        f"- Extracted files: {structure.get('extracted_file_count', report['extracted_file_count'])}",
        f"- Data files found: {structure.get('data_file_count', report['data_file_count'])}",
        f"- Data files analyzed: {structure.get('analyzed_file_count', len(report['analyzed_files']))}",
        f"- Total rows in analyzed files: {structure.get('total_rows_in_analyzed_files', 0)}",
        f"- Columns observed: {', '.join(structure.get('columns_observed') or [])}",
        "",
        "## Completeness",
        "",
        f"- Local files readable: {completeness.get('local_files_readable')}",
        f"- Dataset coverage verified: {completeness.get('dataset_coverage_verified')}",
        f"- Coverage status: {completeness.get('status')}",
        "",
    ]
    if completeness.get("warnings"):
        for warning in completeness["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")
    if model_summary:
        lines.extend(["## Model Interpretation From Local Analysis", "", model_summary, ""])
    lines.extend(["## File Details", ""])
    for item in report["analyzed_files"]:
        lines.extend(
            [
                f"### {Path(item['file']).name}",
                "",
                f"- Rows: {item.get('row_count', 0)}",
                f"- Columns: {', '.join(item.get('columns') or [])}",
                "",
            ]
        )
        if item.get("numeric_stats"):
            lines.append("### Numeric Stats")
            for field, stats in item["numeric_stats"].items():
                lines.append(f"- `{field}`: count={stats['count']}, min={stats['min']}, max={stats['max']}, mean={stats['mean']}")
            lines.append("")
        if item.get("categorical_counts"):
            lines.append("### Categorical Counts")
            for field, counts in item["categorical_counts"].items():
                rendered = ", ".join(f"{key}={value}" for key, value in counts.items())
                lines.append(f"- `{field}`: {rendered}")
            lines.append("")
        if item.get("error"):
            lines.append(f"- Error: {item['error']}")
        lines.extend(["### Sample Rows", "", "```json", json.dumps(item.get("sample_rows") or [], ensure_ascii=False, indent=2), "```", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


async def analyze_downloaded_resource(
    config: dict,
    download_dir: Path,
    skip_model_summary: bool = False,
    analysis_dir: Path | None = None,
) -> dict:
    analysis_dir = analysis_dir or download_dir
    analysis_dir.mkdir(parents=True, exist_ok=True)
    targets = manifest_downloaded_files(download_dir, analysis_dir)
    if not targets:
        latest = latest_downloaded_file(download_dir)
        targets = [latest] if latest else []
    if not targets:
        message = f"No downloaded resource found in {download_dir}."
        print(message)
        return {"ok": False, "message": message}

    extracted_files: list[Path] = []
    data_files: list[Path] = []
    resource_types: list[str] = []
    for target in targets:
        target_type, target_extracted_files, target_data_files = prepare_for_analysis(target, download_dir)
        resource_types.append(target_type)
        extracted_files.extend(target_extracted_files)
        data_files.extend(target_data_files)
    extracted_files = list(dict.fromkeys(extracted_files))
    data_files = list(dict.fromkeys(data_files))
    resource_type = resource_types[0] if len(set(resource_types)) == 1 else "multiple"
    analyzed_files = [analysis for path in data_files[:30] if (analysis := analyze_data_file(path))]
    completeness_warnings = sorted(
        set(
            warning
            for target in targets
            for warning in shard_completeness_warnings(target, data_files, analyzed_files)
        )
    )
    candidate_warnings, observed_candidate_evidence = observed_candidate_completeness_warnings(download_dir, analysis_dir)
    completeness_warnings = sorted(set([*completeness_warnings, *candidate_warnings]))
    report = {
        "downloaded_resource": str(targets[0]),
        "downloaded_resources": [str(target) for target in targets],
        "resource_type": resource_type,
        "extracted_file_count": len(extracted_files),
        "extracted_files_sample": [str(path) for path in extracted_files[:30]],
        "data_file_count": len(data_files),
        "data_files_sample": [str(path) for path in data_files[:30]],
        "analyzed_files": analyzed_files,
        "completeness_warnings": completeness_warnings,
        "observed_candidate_evidence": observed_candidate_evidence,
    }
    report["dataset_summary"] = build_dataset_summary(report, download_dir, analysis_dir)
    report["authoritative_summary"] = authoritative_summary_text(report)

    model_summary = ""
    if not skip_model_summary:
        try:
            model_summary = await model_summarize(config, report)
        except Exception as exc:
            model_summary = f"Model summary skipped because the model call failed: {exc}"

    json_path = analysis_dir / DEFAULT_REPORT_JSON
    md_path = analysis_dir / DEFAULT_REPORT_MD
    json_path.write_text(json.dumps({**report, "model_summary": model_summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(md_path, report, model_summary)

    print(
        "Analysis complete: "
        f"type={resource_type} extracted={len(extracted_files)} data_files={len(data_files)} analyzed={len(analyzed_files)}"
    )
    if completeness_warnings:
        print("  completeness_warnings:")
        for warning in completeness_warnings:
            print(f"    - {warning}")
    print(f"  resources={', '.join(str(target) for target in targets)}")
    print(f"  json={json_path}")
    print(f"  markdown={md_path}")
    print("  verified_summary:")
    for line in report["authoritative_summary"].splitlines():
        print(f"    {line}")
    if model_summary:
        print(f"  model_summary={model_summary[:300]}")
    return {"ok": True, "report": report, "json": str(json_path), "markdown": str(md_path)}
