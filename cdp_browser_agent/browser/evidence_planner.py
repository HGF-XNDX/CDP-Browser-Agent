from __future__ import annotations

import json
import re

from ..model_client import chat_completion


_VERSION_QUALIFIER_RE = re.compile(
    r"[（(]\s*(?:19|20)\d{2}\s*年?"
    r"(?:修正|修订|修改|施行|实施|版|版本|文本|通过|公布)?"
    r"(?:版|本)?\s*[）)]"
    r"|(?<!第)(?:19|20)\d{2}年(?:修正|修订|修改|版|版本|文本|施行|实施)?"
)


def _strip_version_qualifiers(text: str) -> str:
    value = _VERSION_QUALIFIER_RE.sub("", str(text or ""))
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _version_agnostic_request(request: dict) -> dict:
    query = _strip_version_qualifiers(request.get("query") or "")
    desired = _strip_version_qualifiers(request.get("desired_evidence") or "")
    reason = str(request.get("reason") or "")
    version_intent = re.compile(
        r"(?:19|20)\d{2}年|旧法|旧版|旧版本|历史版本|新法|新版|新版本|版本|修正版|修订版"
    )
    if version_intent.search(desired):
        desired = "该法律任一权威完整文本及所需条文原文，不限定修订年份或版本。"
    if version_intent.search(reason):
        reason = (
            "本地未获得所需法律条文原文。按任务规则不区分版本或时间，"
            "获取该法律任一权威完整文本即可。"
        )
    return {
        **request,
        "query": query,
        "desired_evidence": desired,
        "reason": reason,
    }


def _resource_title(request: dict) -> str:
    text = " ".join(str(request.get(key) or "") for key in ("query", "desired_evidence", "reason"))
    match = re.search(r"《[^》]{2,80}》", text)
    return match.group(0) if match else str(request.get("query") or "补充证据")


def _fallback_packages(requests: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for request in requests:
        grouped.setdefault(_resource_title(request), []).append(request)
    packages = []
    for index, (title, items) in enumerate(grouped.items(), 1):
        original_queries = [str(item.get("query") or "") for item in items]
        if title.startswith("《"):
            query = f"{title} 权威现行完整原文"
            desired = f"获取可保存并检索的完整权威文本，至少覆盖以下需要：{'；'.join(original_queries)}"
        else:
            query = "；".join(original_queries)
            desired = "获取能够共同覆盖这些缺失信息的权威资料。"
        packages.append(
            {
                "request_id": f"package_{index}",
                "resource_title": title,
                "query": query,
                "reason": "将同一来源资料的缺失证据合并采集，减少重复搜索并为后续处理提供完整上下文。",
                "desired_evidence": desired,
                "covered_request_ids": [str(item.get("request_id") or "") for item in items],
                "original_queries": original_queries,
            }
        )
    return packages


def _validated_packages(candidate: dict, requests: list[dict]) -> list[dict]:
    expected = {str(item.get("request_id") or "") for item in requests}
    packages: list[dict] = []
    covered: set[str] = set()
    for index, item in enumerate(candidate.get("packages") or [], 1):
        ids = [str(value) for value in item.get("covered_request_ids") or [] if str(value) in expected and str(value) not in covered]
        if not ids or not str(item.get("query") or "").strip():
            continue
        grouped_ids: dict[str, list[str]] = {}
        for request in requests:
            request_id = str(request.get("request_id") or "")
            if request_id in ids:
                grouped_ids.setdefault(_resource_title(request), []).append(request_id)
        for split_index, (title, title_ids) in enumerate(grouped_ids.items(), 1):
            original_queries = [
                str(request.get("query") or "")
                for request in requests
                if str(request.get("request_id") or "") in title_ids
            ]
            split_source = len(grouped_ids) > 1
            packages.append(
                {
                    "request_id": str(item.get("package_id") or f"package_{index}") + (f"_{split_index}" if split_source else ""),
                    "resource_title": title,
                    "query": f"{title} 权威现行完整原文" if split_source and title.startswith("《") else str(item["query"]),
                    "reason": str(item.get("reason") or "按共同来源合并收集证据。"),
                    "desired_evidence": (
                        f"获取可保存并检索的完整权威文本，至少覆盖以下需要：{'；'.join(original_queries)}"
                        if split_source and title.startswith("《")
                        else str(item.get("desired_evidence") or "")
                    ),
                    "covered_request_ids": title_ids,
                    "original_queries": original_queries,
                }
            )
        covered.update(ids)
    missing = [request for request in requests if str(request.get("request_id") or "") not in covered]
    if missing:
        packages.extend(_fallback_packages(missing))
    return _merge_equivalent_packages(packages)


def _merge_equivalent_packages(packages: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for package in packages:
        title = str(package.get("resource_title") or "").strip()
        key = re.sub(r"\s+", "", title) or str(package.get("request_id"))
        if key not in merged:
            merged[key] = dict(package)
            continue
        current = merged[key]
        current["covered_request_ids"] = list(dict.fromkeys((current.get("covered_request_ids") or []) + (package.get("covered_request_ids") or [])))
        current["original_queries"] = list(dict.fromkeys((current.get("original_queries") or []) + (package.get("original_queries") or [])))
        if title.startswith("《"):
            current["query"] = f"{title} 权威现行完整原文"
            current["desired_evidence"] = f"获取可保存并检索的完整权威文本，至少覆盖以下需要：{'；'.join(current['original_queries'])}"
            current["reason"] = "同一权威资料中存在多个所需条文，合并为一次完整文本采集以减少重复搜索。"
    return list(merged.values())


async def plan_collection_packages(task: str, requests: list[dict], config: dict) -> tuple[list[dict], str]:
    requests = [_version_agnostic_request(request) for request in requests]
    fallback = _fallback_packages(requests)
    coordinator = config.get("coordinator") or {}
    if not coordinator.get("use_model_collection_planner", True) or len(requests) < 2:
        return _merge_equivalent_packages(fallback), "fallback"
    messages = [
        {
            "role": "system",
            "content": (
                "你是调度智能体中的证据补采规划器。数据处理智能体已经一次性给出本轮全部证据缺口。"
                "请减少浏览器搜索次数：多个缺口如果属于同一法律、指南、报告或数据源，应合并为一次资料采集，"
                "优先请求该资料的完整权威文本或足以覆盖所有缺口的官方章节，而不是逐条搜索。"
                "不同资料来源不得错误合并。输出 JSON："
                '{"packages":[{"package_id":"package_1","resource_title":"...",'
                '"query":"用于浏览器检索的简短资料目标","covered_request_ids":["..."],'
                '"reason":"...","desired_evidence":"..."}]}。'
            ),
        },
        {"role": "user", "content": json.dumps({"task": task, "missing_requests": requests}, ensure_ascii=False)},
    ]
    try:
        raw = await chat_completion(messages, config.get("model") or {})
        packages = _validated_packages(json.loads(raw), requests)
        return [_version_agnostic_request(package) for package in packages], "model"
    except Exception:
        return _merge_equivalent_packages(fallback), "fallback_model_error"
