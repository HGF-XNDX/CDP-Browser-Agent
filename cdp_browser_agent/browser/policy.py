from __future__ import annotations

import base64
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse, urlunparse


ALLOWED_ACTIONS = {
    "click",
    "type",
    "press",
    "scroll",
    "navigate",
    "open_tab",
    "switch_tab",
    "download",
    "save_page",
    "back",
    "wait",
    "observe_vision",
    "done",
    "ask_user",
}

SEARCH_ENGINE_TEMPLATES = {
    "bing": "https://www.bing.com/search?q={query}",
    "duckduckgo": "https://duckduckgo.com/?q={query}",
    "baidu": "https://www.baidu.com/s?wd={query}",
    "quark": "https://quark.sm.cn/s?q={query}",
    "sogou": "https://www.sogou.com/web?query={query}",
    "so": "https://www.so.com/s?q={query}",
}

SEARCH_ENGINES = list(SEARCH_ENGINE_TEMPLATES.values())

MIN_SOURCE_TEXT_FOR_READING = 500

VERIFICATION_KEYWORDS = [
    "captcha",
    "verify you are human",
    "security check",
    "access denied",
    "login required",
    "please log in",
    "\u9a8c\u8bc1\u7801",
    "\u5b89\u5168\u9a8c\u8bc1",
    "\u4eba\u673a\u9a8c\u8bc1",
    "\u8bf7\u767b\u5f55",
]

LOW_VALUE_HOSTS = {
    "console.cloud.tencent.com",
    "passport.baidu.com",
    "wappass.baidu.com",
    "hao123.com",
    "image.baidu.com",
    "map.baidu.com",
    "v.baidu.com",
    "haokan.baidu.com",
}

LOW_VALUE_URL_PATTERNS = [
    "login",
    "passport",
    "captcha",
    "sorry/index",
    "/images/search",
    "tokenhub",
    "console.",
    "signup",
    "register",
    "download",
    "cart",
    "payment",
    "terms",
    "privacy",
]

ORDER_TASK_KEYWORDS = ["订单", "账单", "充值", "消费", "用量", "发票", "order", "billing", "bill", "recharge", "invoice", "usage"]

KNOWN_DATASET_QUERY_TERMS = [
    "webbench",
    "mind2web",
    "webarena",
    "osworld",
    "webvoyager",
    "agentbench",
    "agentgym",
    "miniwob",
    "browsergym",
    "visualwebarena",
]


def ask_user(message: str) -> dict:
    return {"action": "ask_user", "message": message}


def replan_action(reason: str, message: str) -> dict:
    return {"action": "wait", "ms": 500, "reason": reason, "message": message}


def sanitize_search_query(task: str) -> str:
    text = str(task or "")
    text = re.sub(r"[（(][^）)]*(?:密码|password|pass|pwd|账号|account|手机|phone)[^）)]*[）)]", " ", text, flags=re.I)
    text = re.sub(r"(?:密码|password|pass|pwd|验证码|code)\s*(?:是|为|=|:|：)?\s*\S+", " ", text, flags=re.I)
    text = re.sub(r"(?:账号|account|手机|手机号|phone)\s*(?:是|为|=|:|：)?\s*\S+", " ", text, flags=re.I)
    text = re.sub(r"\b\d{6,}\b", " ", text)
    text = re.sub(r"帮我|请帮|请|下载|保存|另存为|原文下载|全文下载|download|save\s+as|save\s+to", " ", text, flags=re.I)
    lowered = text.lower()

    known_terms = []
    for term in KNOWN_DATASET_QUERY_TERMS:
        if term in lowered:
            known_terms.append(term)

    if known_terms:
        primary = known_terms[0]
        query_terms = [primary, "dataset"]
        if "huggingface" in lowered or "hugging face" in lowered:
            query_terms.append("huggingface")
        elif "modelscope" in lowered or "魔搭" in lowered:
            query_terms.append("modelscope")
        elif any(word in lowered for word in ["github", "git hub"]):
            query_terms.append("github")
        return " ".join(list(dict.fromkeys(query_terms))[:4])[:80]

    if any(word in lowered for word in ["浏览器智能体", "网页智能体", "web agent", "browser agent"]):
        known_terms.extend(["browser agent", "benchmark", "dataset"])
    if any(word in lowered for word in ["gui 智能体", "gui-agent", "gui agent", "电脑使用", "computer use"]):
        known_terms.extend(["gui agent", "benchmark", "dataset"])
    if any(word in lowered for word in ["数据集", "dataset", "benchmark", "评测", "基准"]):
        known_terms.extend(["benchmark", "dataset"])
    if any(word in lowered for word in ["github", "git hub"]):
        known_terms.append("site:github.com")
    if "huggingface" in lowered or "hugging face" in lowered:
        known_terms.append("site:huggingface.co")
    for fmt in ["csv", "jsonl", "json", "zip", "tar.gz", "gzip", "tsv"]:
        if fmt in lowered:
            known_terms.append(fmt)

    urls = re.findall(r"https?://[^\s，。；,;]+", text)
    hosts = []
    for url in urls[:2]:
        value = host(url)
        if value:
            hosts.append(f"site:{value}")

    if known_terms or hosts:
        deduped = list(dict.fromkeys([*known_terms, *hosts]))
        query = " ".join(deduped[:10])
        return query[:160]

    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(
        r"(请|执行|一个|资源|获取|测试|搜索|寻找|打开|观察|确认|下载|保存|本地|目录|完成|停止|说明|不要|使用|登录|网站|如果|例如|优先|选择|直接|完整|小型|数据集|资源|字段|文件|类型|后续|分析|公开|无需|账号|验证码|付费)",
        " ",
        text,
        flags=re.I,
    )
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_.:-]{1,}|[\u4e00-\u9fff]{2,}", text)
    filtered = []
    for token in tokens:
        lower = token.lower()
        if lower in {"http", "https", "www", "com", "main"}:
            continue
        filtered.append(token)
    query = " ".join(list(dict.fromkeys(filtered))[:10]).strip()
    return (query[:160] if query else "browser agent benchmark dataset download")


def is_catalog_search_context(observation: dict, element: dict) -> bool:
    url = observation.get("url", "")
    parsed = urlparse(url or "")
    hostname = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    text = element_text_blob(element).lower()
    if hostname == "huggingface.co" and (path.startswith("/datasets") or "dataset" in text):
        return True
    if any(word in text for word in ["dataset", "datasets", "数据集", "catalog", "repository", "repo"]):
        return True
    return False

def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url or "")
        if not parsed.scheme or not parsed.netloc:
            return url or ""
        path = parsed.path.rstrip("/") or "/"
        return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, ""))
    except Exception:
        return url or ""


def decode_base64_url(value: str) -> str:
    try:
        normalized = value.replace("-", "+").replace("_", "/")
        normalized += "=" * ((4 - len(normalized) % 4) % 4)
        return base64.b64decode(normalized).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def normalize_candidate_href(raw_url: str) -> str:
    url = normalize_url(raw_url)
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if hostname.endswith("bing.com") and parsed.path.startswith("/ck/"):
            encoded = (parse_qs(parsed.query).get("u") or [""])[0]
            candidates = [encoded, re.sub(r"^a1", "", encoded, flags=re.I)]
            for candidate in candidates:
                decoded = decode_base64_url(candidate)
                if decoded.startswith("http://") or decoded.startswith("https://"):
                    return normalize_url(decoded)
                if decoded.startswith("/"):
                    return normalize_url(f"{parsed.scheme}://{parsed.hostname}{decoded}")
    except Exception:
        return url
    return url


def host(url: str) -> str:
    try:
        return urlparse(url or "").hostname or ""
    except Exception:
        return ""


def is_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except Exception:
        return False


def explicit_urls_from_task(task: str) -> set[str]:
    return {
        normalize_url(url)
        for url in re.findall(r"https?://[^\s，。；,;'\"<>）)]+", str(task or ""), flags=re.I)
        if normalize_url(url)
    }


def visible_href_urls(observation: dict) -> set[str]:
    urls = set()
    for element in observation.get("elements", []) or []:
        href = normalize_candidate_href(element.get("href", ""))
        if is_http_url(href):
            urls.add(href)
    return urls


def observed_known_urls(request: dict | None) -> set[str]:
    urls = set()
    if not request:
        return urls
    urls.update(explicit_urls_from_task(request.get("task", "")))
    for source in request.get("sources", []) or []:
        url = normalize_url(source.get("url", ""))
        if is_http_url(url):
            urls.add(url)
    for item in request.get("history", []) or []:
        page_url = normalize_url(item.get("url", ""))
        if is_http_url(page_url):
            urls.add(page_url)
        result = item.get("result") or {}
        result_url = normalize_url(result.get("url", ""))
        if result.get("ok") is not False and is_http_url(result_url):
            urls.add(result_url)
        action = item.get("action") or {}
        href = normalize_candidate_href(action.get("href", ""))
        if is_http_url(href):
            urls.add(href)
    urls.update(visible_href_urls(request.get("observation") or {}))
    return urls


def generated_search_urls(request: dict | None) -> set[str]:
    if not request:
        return set()
    query = quote(sanitize_search_query(request.get("task", "")))
    return {
        normalize_url(template.replace("{query}", query))
        for template in list(dict.fromkeys([*configured_search_engines(request), *SEARCH_ENGINES]))
    }


def has_url_provenance(action: dict, observation: dict, request: dict | None) -> bool:
    if not request or action.get("action") not in {"navigate", "open_tab"}:
        return True
    target_url = normalize_candidate_href(action.get("url", ""))
    if not is_http_url(target_url):
        return True
    return (
        target_url in visible_href_urls(observation)
        or target_url in observed_known_urls(request)
        or normalize_url(action.get("url", "")) in generated_search_urls(request)
    )


def is_search_page(url: str) -> bool:
    hostname = host(url).lower()
    return any(domain in hostname for domain in ["baidu.com", "bing.com", "duckduckgo.com", "google.com", "sogou.com", "sm.cn", "so.com"])


def task_refers_current_page(task: str) -> bool:
    return bool(
        re.search(
            r"(当前页面|这个页面|现在这个页面|本页|当前网页|这个网页|当前打开的页面|current page|this page|current tab|this tab|here)",
            str(task or ""),
            flags=re.I,
        )
    )


def task_explicitly_requests_search(task: str) -> bool:
    return bool(re.search(r"(搜索|搜一下|联网查|web search|search the web|look up online)", str(task or ""), flags=re.I))


def task_explicitly_requests_download(task: str) -> bool:
    return bool(re.search(r"(下载|保存到|保存至|download|save\\s+to|save\\s+as)", str(task or ""), flags=re.I))


def model_first_strategy(request: dict | None) -> bool:
    return bool(((request or {}).get("agent_settings") or {}).get("model_first_strategy", True))


DATA_RESOURCE_EXTENSIONS = (
    ".csv",
    ".json",
    ".jsonl",
    ".tsv",
    ".parquet",
    ".zip",
    ".tar.gz",
    ".tgz",
    ".gz",
)


def element_text_blob(element: dict) -> str:
    return " ".join(
        str(element.get(key) or "")
        for key in (
            "text",
            "ariaLabel",
            "placeholder",
            "label",
            "name",
            "title",
            "description",
            "actionHint",
            "nearbyText",
            "containerText",
            "formContext",
        )
    )


def is_huggingface_dataset_page(url: str) -> bool:
    parsed = urlparse(url or "")
    return (parsed.hostname or "").lower() == "huggingface.co" and parsed.path.startswith("/datasets/")


def resource_filename_from_url(url: str) -> str:
    path = urlparse(url or "").path
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._") or "downloaded_resource"


def has_data_resource_extension(value: str) -> bool:
    lowered = (value or "").lower()
    return any(lowered.endswith(ext) or f"{ext}?" in lowered for ext in DATA_RESOURCE_EXTENSIONS)


def is_direct_resource_url(url: str) -> bool:
    if not is_http_url(url):
        return False
    parsed = urlparse(url or "")
    path = parsed.path.lower()
    hostname = (parsed.hostname or "").lower()
    return has_data_resource_extension(path) or (
        hostname in {"raw.githubusercontent.com", "githubusercontent.com"}
        and bool(path.rsplit("/", 1)[-1])
    )


def is_direct_download_href(href: str, text: str = "") -> bool:
    lowered_href = (href or "").lower()
    parsed = urlparse(href or "")
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if "/resolve/" in lowered_href and (has_data_resource_extension(lowered_href) or "/data/" in lowered_href):
        return True
    if hostname == "raw.githubusercontent.com" and has_data_resource_extension(path):
        return True
    if hostname.endswith("github.com") and "/raw/" in path and has_data_resource_extension(path):
        return True
    return False


def verified_downloads(history: list[dict] | None) -> list[dict]:
    downloads = []
    for item in history or []:
        action = item.get("action") or {}
        result = item.get("result") or {}
        path = result.get("path") or result.get("filename") or ""
        if action.get("action") not in {"download", "save_page"} and result.get("message") not in {"downloaded", "saved_page"}:
            continue
        if not result.get("ok"):
            continue
        if path:
            try:
                file_path = Path(path)
                if file_path.exists() and file_path.is_file() and file_path.stat().st_size > 0:
                    downloads.append({"path": str(file_path), "bytes": file_path.stat().st_size, "url": result.get("url") or action.get("url", "")})
                    continue
            except Exception:
                pass
        if int(result.get("bytes") or 0) > 0:
            downloads.append({"path": path, "bytes": int(result.get("bytes") or 0), "url": result.get("url") or action.get("url", "")})
    return downloads


def has_verified_download(request: dict | None) -> bool:
    return bool(verified_downloads((request or {}).get("history") or []))


def task_allows_saving_current_page(task: str) -> bool:
    return task_explicitly_requests_download(task) and bool(
        re.search(r"(原文|全文|正文|页面|网页|法律|法规|法条|文档|article|text|page)", str(task or ""), flags=re.I)
    )


def current_page_can_be_saved(observation: dict) -> bool:
    if is_search_page(observation.get("url", "")) or is_login_or_account_page(observation):
        return False
    text = observation.get("observedText") or observation.get("visibleText") or observation.get("viewportText") or ""
    return len(str(text or "").strip()) >= 500


def filename_from_task(task: str, fallback: str = "saved_page.txt") -> str:
    cleaned = sanitize_search_query(task)
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", cleaned)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")[:80]
    return f"{cleaned or Path(fallback).stem}.txt"


def filename_from_title(title: str, fallback: str = "saved_page.txt") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", str(title or ""))
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")[:80]
    return f"{cleaned or Path(fallback).stem}.txt"


def current_page_already_saved(request: dict | None) -> bool:
    current_url = normalize_url((request or {}).get("observation", {}).get("url", ""))
    if not current_url:
        return False
    for item in (request or {}).get("history", []) or []:
        action = item.get("action") or {}
        result = item.get("result") or {}
        if (
            action.get("action") == "save_page"
            and result.get("ok") is not False
            and result.get("message") == "saved_page"
            and normalize_url(result.get("url") or item.get("url", "")) == current_url
        ):
            return True
    return False


def current_page_looks_like_requested_legal_text(task: str, observation: dict) -> bool:
    text = "\n".join(
        [
            str(task or ""),
            observation.get("title", ""),
            observation.get("url", ""),
            observation.get("visibleText", ""),
            observation.get("viewportText", ""),
        ]
    )
    return bool(re.search(r"(专利法|实施细则|法律|法规|规章|条例|解释|解读|释义|patent law|regulation|interpretation)", text, flags=re.I))


def should_save_current_page_before_leaving(action: dict, observation: dict, request: dict | None) -> bool:
    if not request or action.get("action") not in {"navigate", "open_tab", "back"}:
        return False
    return (
        task_allows_saving_current_page(request.get("task", ""))
        and current_page_can_be_saved(observation)
        and current_page_looks_like_requested_legal_text(request.get("task", ""), observation)
        and not current_page_already_saved(request)
    )


def is_malformed_search_url(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
        hostname = (parsed.hostname or "").lower()
        values = parse_qs(parsed.query)
        if "baidu.com" in hostname:
            return parsed.path.startswith("/swd=") or (parsed.path == "/s" and not values.get("wd"))
        if "bing.com" in hostname:
            return parsed.path.startswith("/searchq=") or (parsed.path == "/search" and not values.get("q"))
        if "sogou.com" in hostname:
            return "webquery=" in parsed.path or (parsed.path.endswith("/web") and not values.get("query"))
    except Exception:
        return False
    return False


def is_hf_data_folder_href(href: str) -> bool:
    path = urlparse(href or "").path.rstrip("/")
    return bool(re.search(r"/datasets/[^/]+/[^/]+/tree/[^/]+/(data|datasets)$", path, flags=re.I))


def is_hf_blob_data_file_href(href: str) -> bool:
    parsed = urlparse(href or "")
    return parsed.path.startswith("/datasets/") and "/blob/" in parsed.path and has_data_resource_extension(parsed.path)


def hf_blob_to_download_url(href: str) -> str:
    parsed = urlparse(href or "")
    path = parsed.path.replace("/blob/", "/resolve/", 1)
    query = parsed.query
    if "download=true" not in query:
        query = f"{query}&download=true" if query else "download=true"
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query, ""))


def many_peer_data_files(observation: dict) -> bool:
    file_links = []
    for element in observation.get("elements", []) or []:
        href = normalize_candidate_href(element.get("href", ""))
        if is_hf_blob_data_file_href(href):
            file_links.append(resource_filename_from_url(href).lower())
    return len(set(file_links)) > 3


def candidate_dataset_resource_action(observation: dict, request: dict | None, reason: str = "prefer_dataset_file_or_download") -> dict | None:
    if not request or not task_explicitly_requests_download(request.get("task", "")):
        return None
    current_url = observation.get("url", "")
    if not is_huggingface_dataset_page(current_url):
        return None

    elements = observation.get("elements", []) or []
    direct_downloads = []
    for element in elements:
        href = normalize_candidate_href(element.get("href", ""))
        text = element_text_blob(element)
        if is_http_url(href) and is_direct_download_href(href, text):
            direct_downloads.append((element, href))
    if len(direct_downloads) == 1:
        _element, href = direct_downloads[0]
        return {
            "action": "download",
            "url": href,
            "filename": resource_filename_from_url(href),
            "reason": reason,
            "message": "A direct dataset download link is visible; downloading it instead of clicking nearby navigation.",
        }
    if len(direct_downloads) > 1:
        return None

    current_path = urlparse(current_url or "").path.rstrip("/")
    folder_candidates = []
    for element in elements:
        href = normalize_candidate_href(element.get("href", ""))
        if not element.get("id") or not is_http_url(href) or not is_hf_data_folder_href(href):
            continue
        if normalize_url(href) == normalize_url(current_url):
            continue
        text = element_text_blob(element).lower()
        score = 10
        if "data" in text or "dataset" in text:
            score += 4
        if element.get("isInViewport"):
            score += 1
        folder_candidates.append((score, element, href))
    if folder_candidates:
        folder_candidates.sort(key=lambda item: item[0], reverse=True)
        element = folder_candidates[0][1]
        return {
            "action": "click",
            "target_id": element["id"],
            "href": folder_candidates[0][2],
            "reason": reason,
            "message": "Opening the actual data folder link from the dataset file tree.",
        }

    if many_peer_data_files(observation):
        return None

    file_candidates = []
    for element in elements:
        href = normalize_candidate_href(element.get("href", ""))
        if not element.get("id") or not is_http_url(href) or not is_hf_blob_data_file_href(href):
            continue
        score = 10
        path = urlparse(href).path.lower()
        if "/data/" in path or current_path.endswith("/data"):
            score += 4
        if re.search(r"00000-of-00001", path):
            score += 5
        if element.get("isInViewport"):
            score += 1
        file_candidates.append((score, element, href))
    if file_candidates:
        file_candidates.sort(key=lambda item: item[0], reverse=True)
        href = file_candidates[0][2]
        return {
            "action": "download",
            "url": hf_blob_to_download_url(href),
            "filename": resource_filename_from_url(href),
            "reason": reason,
            "message": "A single dataset file is visible in the file tree; using the direct resolve URL.",
        }

    return None


def current_page_has_usable_context(observation: dict) -> bool:
    url = observation.get("url", "")
    if not is_http_url(url):
        return False
    text = " ".join(
        str(observation.get(key) or "")
        for key in ("visibleText", "viewportText", "observedText", "semanticTree", "cleanedHtml", "title")
    )
    return len(observation.get("elements") or []) > 0 or len(text.strip()) >= 80


def configured_search_engines(request: dict) -> list[str]:
    selected = ((request.get("model_settings") or {}).get("searchEngine") or "auto").strip()
    if selected != "auto" and selected in SEARCH_ENGINE_TEMPLATES:
        return [SEARCH_ENGINE_TEMPLATES[selected]]
    return SEARCH_ENGINES


def is_error_page(observation: dict) -> bool:
    title = (observation.get("title") or "").strip().lower()
    text = (observation.get("visibleText") or "").strip().lower()
    url = (observation.get("url") or "").lower()
    return (
        title in {"404", "404: not_found", "browser error page"}
        or "browser error:" in text[:300]
        or "not_found" in title
        or "/404" in url
    )


def is_source_page(observation: dict) -> bool:
    url = observation.get("url", "")
    if not is_http_url(url) or is_search_page(url) or is_error_page(observation):
        return False
    return len((observation.get("visibleText") or "").strip()) >= MIN_SOURCE_TEXT_FOR_READING


def is_search_redirect_candidate(url: str) -> bool:
    parsed = urlparse(url or "")
    hostname = (parsed.hostname or "").lower()
    return (hostname.endswith("baidu.com") and parsed.path.startswith("/link")) or (
        hostname.endswith("bing.com") and parsed.path.startswith("/ck/")
    )


def is_low_value_candidate(url: str) -> bool:
    parsed = urlparse(url or "")
    hostname = (parsed.hostname or "").lower()
    lowered = (url or "").lower()
    if not hostname:
        return True
    if hostname in LOW_VALUE_HOSTS:
        return True
    if hostname.endswith(".hao123.com"):
        return True
    if any(pattern in lowered for pattern in LOW_VALUE_URL_PATTERNS):
        return True
    if hostname.endswith("bing.com") and parsed.path.startswith("/images"):
        return True
    if hostname.endswith("google.com") and "/sorry/" in parsed.path:
        return True
    return False


def usable_sources(sources: list[dict]) -> list[dict]:
    seen = set()
    usable = []
    for source in sources or []:
        url = normalize_url(source.get("url", ""))
        if not is_http_url(url) or is_search_page(url) or is_low_value_candidate(url) or url in seen:
            continue
        if len((source.get("snippet") or "").strip()) < 80:
            continue
        seen.add(url)
        usable.append(source)
    return usable


def visited_urls(history: list[dict], sources: list[dict]) -> set[str]:
    urls = {normalize_url(source.get("url", "")) for source in sources or []}
    for item in history or []:
        if item.get("url"):
            urls.add(normalize_url(item["url"]))
        action = item.get("action") or {}
        if action.get("url"):
            urls.add(normalize_url(action["url"]))
        if action.get("href"):
            urls.add(normalize_url(action["href"]))
    return urls


def task_terms(task: str) -> list[str]:
    terms = [term.lower() for term in re.split(r"[^\w\u4e00-\u9fff]+", sanitize_search_query(task)) if len(term) >= 2][:12]
    if any(keyword in (task or "").lower() for keyword in ORDER_TASK_KEYWORDS):
        terms.extend(["订单", "账单", "充值", "消费", "用量", "发票", "order", "billing", "bill", "recharge", "invoice", "usage", "record"])
    return list(dict.fromkeys(terms))[:24]


def is_login_or_account_page(observation: dict) -> bool:
    if is_search_page(observation.get("url", "")):
        return False
    text = "\n".join(
        [
            observation.get("url", ""),
            observation.get("title", ""),
            (observation.get("visibleText") or "")[:1200],
        ]
    ).lower()
    return any(
        keyword in text
        for keyword in [
            "login",
            "unified-login",
            "signin",
            "sign in",
            "account",
            "user-center",
            "passport",
            "oauth",
            "登录",
            "账户",
            "账号",
            "用户中心",
        ]
    )


def has_readable_business_context(observation: dict) -> bool:
    text = "\n".join(
        [
            observation.get("url", ""),
            observation.get("title", ""),
            observation.get("visibleText", ""),
            observation.get("semanticTree", ""),
        ]
    ).lower()
    if len((observation.get("visibleText") or "").strip()) < 20 and len((observation.get("semanticTree") or "").strip()) < 20:
        return False
    return any(
        keyword in text
        for keyword in [
            "user-center",
            "account",
            "dashboard",
            "console",
            "order",
            "billing",
            "invoice",
            "usage",
            "\u8d26\u6237",
            "\u7528\u6237\u4e2d\u5fc3",
            "\u8ba2\u5355",
            "\u8d26\u5355",
            "\u5145\u503c",
            "\u6d88\u8d39",
            "\u7528\u91cf",
            "\u53d1\u7968",
            "\u8bb0\u5f55",
        ]
    )


def is_password_like_element(element: dict) -> bool:
    text = " ".join(
        [
            element.get("type", ""),
            element.get("text", ""),
            element.get("placeholder", ""),
            element.get("ariaLabel", ""),
            element.get("name", ""),
            element.get("label", ""),
        ]
    ).lower()
    return element.get("type") == "password" or any(word in text for word in ["password", "passwd", "pwd", "密码"])


def allow_password_input(request: dict | None) -> bool:
    if not request:
        return False
    model_settings = request.get("model_settings") or {}
    agent_settings = request.get("agent_settings") or {}
    return bool(
        model_settings.get("allowPasswordInput")
        or model_settings.get("allow_password_input")
        or agent_settings.get("allow_password_input")
        or agent_settings.get("allowPasswordInput")
    )


AGREEMENT_KEYWORDS = [
    "agree",
    "agreement",
    "terms",
    "privacy",
    "policy",
    "consent",
    "\u540c\u610f",
    "\u534f\u8bae",
    "\u6761\u6b3e",
    "\u9690\u79c1",
    "\u653f\u7b56",
    "\u6211\u5df2\u9605\u8bfb",
]


def is_agreement_element(element: dict) -> bool:
    local_text = " ".join(
        [
            element.get("id", ""),
            element.get("tag", ""),
            element.get("type", ""),
            element.get("role", ""),
            element.get("state", ""),
            element.get("text", ""),
            element.get("placeholder", ""),
            element.get("ariaLabel", ""),
            element.get("name", ""),
            element.get("label", ""),
        ]
    ).lower()
    nearby_text = " ".join([local_text, element.get("nearbyText", "")]).lower()
    context_text = " ".join([nearby_text, element.get("containerText", ""), element.get("formContext", "")]).lower()
    actual_choice = (
        element.get("type") in {"checkbox", "radio"}
        or str(element.get("id", "")).startswith(("checkbox_", "radio_"))
        or element.get("role") in {"checkbox", "radio", "switch"}
    )
    small_control = (
        str(element.get("id", "")).startswith("btn_")
        and element.get("tag") in {"button", "label", "span", "div"}
        and float(element.get("width") or 0) <= 56
        and float(element.get("height") or 0) <= 56
    )
    if element.get("href"):
        return False
    if actual_choice:
        return any(keyword.lower() in context_text for keyword in AGREEMENT_KEYWORDS)
    if small_control:
        return any(keyword.lower() in nearby_text for keyword in AGREEMENT_KEYWORDS)
    return False


SUBMIT_KEYWORDS = [
    "login",
    "log in",
    "sign in",
    "submit",
    "continue",
    "next",
    "\u767b\u5f55",
    "\u7acb\u5373\u767b\u5f55",
    "\u63d0\u4ea4",
    "\u7ee7\u7eed",
    "\u4e0b\u4e00\u6b65",
]


def is_submit_element(element: dict) -> bool:
    if not element.get("id") or element.get("disabled") or element.get("href"):
        return False
    if element.get("type") in {"password", "text", "email", "tel", "search", "checkbox", "radio"}:
        return False
    local_text = " ".join(
        [
            element.get("id", ""),
            element.get("tag", ""),
            element.get("role", ""),
            element.get("type", ""),
            element.get("text", ""),
            element.get("ariaLabel", ""),
            element.get("placeholder", ""),
            element.get("label", ""),
            element.get("name", ""),
        ]
    ).lower()
    text = " ".join([local_text, element.get("nearbyText", "")]).lower()
    if any(keyword.lower() in local_text for keyword in AGREEMENT_KEYWORDS):
        return False
    if any(keyword.lower() in local_text for keyword in PASSWORD_MODE_KEYWORDS):
        return False
    looks_clickable = (
        str(element.get("id", "")).startswith(("btn_", "button_"))
        or element.get("role") in {"button", "menuitem", "tab"}
        or element.get("tag") in {"button", "input", "div", "span"}
    )
    return looks_clickable and any(keyword.lower() in text for keyword in SUBMIT_KEYWORDS)


def candidate_submit_action(observation: dict, reason: str = "submit_form_after_prerequisites") -> dict | None:
    candidates = []
    for element in observation.get("elements", []) or []:
        if is_submit_element(element):
            score = 1
            text = " ".join(
                [
                    element.get("text", ""),
                    element.get("ariaLabel", ""),
                    element.get("label", ""),
                    element.get("nearbyText", ""),
                    element.get("formContext", ""),
                ]
            ).lower()
            if any(word in text for word in ["login", "log in", "sign in", "\u767b\u5f55", "\u7acb\u5373\u767b\u5f55"]):
                score += 4
            if element.get("isInViewport"):
                score += 1
            candidates.append((score, element))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return None
    return {
        "action": "click",
        "target_id": candidates[0][1]["id"],
        "reason": reason,
        "message": "Submitting the form after visible prerequisites are satisfied.",
    }


PASSWORD_MODE_KEYWORDS = [
    "password login",
    "login with password",
    "password",
    "\u5bc6\u7801\u767b\u5f55",
    "\u5bc6\u7801",
    "\u8d26\u53f7\u5bc6\u7801",
]


def task_wants_password_login(request: dict | None) -> bool:
    if not request:
        return False
    text = " ".join([request.get("task", ""), str((request.get("model_settings") or {}).get("allowPasswordInput", ""))]).lower()
    return any(keyword.lower() in text for keyword in PASSWORD_MODE_KEYWORDS)


def has_visible_password_input(observation: dict) -> bool:
    return any(
        element.get("tag") in {"input", "textarea"} and (element.get("type") == "password" or is_password_like_element(element))
        for element in observation.get("elements", []) or []
    )


def candidate_password_mode_action(observation: dict, request: dict | None, reason: str = "switch_to_password_login") -> dict | None:
    if not task_wants_password_login(request) or has_visible_password_input(observation):
        return None
    candidates = []
    for element in observation.get("elements", []) or []:
        if not element.get("id") or element.get("disabled") or element.get("href"):
            continue
        if element.get("type") in {"password", "text", "email", "tel", "search", "checkbox", "radio"}:
            continue
        if is_agreement_element(element):
            continue
        text = " ".join(
            [
                element.get("id", ""),
                element.get("tag", ""),
                element.get("role", ""),
                element.get("text", ""),
                element.get("ariaLabel", ""),
                element.get("label", ""),
                element.get("nearbyText", ""),
                element.get("containerText", ""),
                element.get("formContext", ""),
            ]
        ).lower()
        if not any(keyword.lower() in text for keyword in PASSWORD_MODE_KEYWORDS):
            continue
        score = 1
        if element.get("role") in {"tab", "button"} or element.get("tag") in {"button", "span", "div"}:
            score += 2
        if element.get("isInViewport"):
            score += 1
        candidates.append((score, element))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return None
    return {
        "action": "click",
        "target_id": candidates[0][1]["id"],
        "reason": reason,
        "message": "Switching to password login mode before filling credentials.",
    }


def candidate_agreement_action(observation: dict, reason: str = "accept_required_agreement") -> dict | None:
    for element in observation.get("elements", []) or []:
        if element.get("disabled") or element.get("checked") is True:
            continue
        if element.get("href"):
            continue
        if is_agreement_element(element) and element.get("id"):
            return {
                "action": "click",
                "target_id": element["id"],
                "reason": reason,
                "message": "Clicking the required agreement checkbox before submitting the login form.",
            }
    return None


def recently_clicked_target(request: dict | None, observation: dict, target_id: str, window: int = 8) -> bool:
    if not request or not target_id:
        return False
    current_url = normalize_url(observation.get("url", ""))
    for item in (request.get("history") or [])[-window:]:
        item_action = item.get("action") or {}
        if (
            normalize_url(item.get("url", "")) == current_url
            and item_action.get("action") == "click"
            and item_action.get("target_id") == target_id
        ):
            return True
    return False


def recently_clicked_agreement(request: dict | None, observation: dict, window: int = 8) -> bool:
    if not request:
        return False
    elements = {element.get("id"): element for element in observation.get("elements", []) or []}
    current_url = normalize_url(observation.get("url", ""))
    for item in (request.get("history") or [])[-window:]:
        item_action = item.get("action") or {}
        target_id = item_action.get("target_id")
        if normalize_url(item.get("url", "")) != current_url or item_action.get("action") != "click":
            continue
        if target_id and is_agreement_element(elements.get(target_id, {})):
            return True
    return False


def link_score(element: dict, href: str, terms: list[str]) -> int:
    text = " ".join(
        [
            element.get("text", ""),
            element.get("ariaLabel", ""),
            element.get("placeholder", ""),
            element.get("label", ""),
            element.get("name", ""),
            element.get("description", ""),
            element.get("actionHint", ""),
            element.get("nearbyText", ""),
            element.get("containerText", ""),
            href,
        ]
    ).lower()
    score = 0
    for term in terms:
        if term in text:
            score += 5
    if any(word in text for word in ["openclaw", "cdp", "browser", "\u81ea\u52a8", "\u6d4f\u89c8\u5668", "\u6559\u7a0b", "\u539f\u7406"]):
        score += 3
    if any(word in text for word in ORDER_TASK_KEYWORDS):
        score += 8
    if any(word in text for word in ["dataset", "datasets", "data", "files", "files and versions", "download", "raw", "数据集", "数据", "文件", "下载"]):
        score += 4
    if any(word in text for word in ["terms", "privacy", "服务条款", "隐私"]):
        score -= 10
    if any(word in text for word in ["大家还在搜", "相关搜索", "people also search", "related searches"]):
        score -= 8
    if any(word in text for word in ["\u56fe\u7247", "image", "\u767b\u5f55", "login", "\u63a7\u5236\u53f0", "console", "\u5e7f\u544a", "ad"]):
        score -= 8
    if element.get("isInViewport"):
        score += 1
    return score


def candidate_link_elements(observation: dict, visited: set[str], task: str = "") -> list[dict]:
    links = []
    seen = set()
    terms = task_terms(task)
    for element in observation.get("elements", []) or []:
        href = normalize_candidate_href(element.get("href", ""))
        if not is_http_url(href) or href in visited or href in seen:
            continue
        if is_low_value_candidate(href):
            continue
        if is_search_page(href) and not is_search_redirect_candidate(href):
            continue
        seen.add(href)
        links.append((link_score(element, href, terms), element, href))
    links.sort(key=lambda item: item[0], reverse=True)
    return [{"element": element, "href": href, "score": score} for score, element, href in links if score >= -2]


def candidate_links(observation: dict, visited: set[str], task: str = "") -> list[str]:
    return [item["href"] for item in candidate_link_elements(observation, visited, task)]


def candidate_click_action(observation: dict, visited: set[str], task: str, reason: str, message: str = "") -> dict | None:
    for item in candidate_link_elements(observation, visited, task):
        target_id = item["element"].get("id")
        if target_id:
            action = {"action": "click", "target_id": target_id, "reason": reason, "href": item["href"]}
            if message:
                action["message"] = message
            return action
    return None


def element_action_score(element: dict, terms: list[str]) -> int:
    text = " ".join(
        [
            element.get("id", ""),
            element.get("role", ""),
            element.get("type", ""),
            element.get("text", ""),
            element.get("ariaLabel", ""),
            element.get("placeholder", ""),
            element.get("label", ""),
            element.get("name", ""),
            element.get("description", ""),
            element.get("actionHint", ""),
            element.get("nearbyText", ""),
            element.get("containerText", ""),
            element.get("formContext", ""),
        ]
    ).lower()
    if not element.get("id") or element.get("disabled") or element.get("type") in {"password", "text", "email", "tel", "search"}:
        return -100
    score = 0
    for term in terms:
        if term and term in text:
            score += 5
    if any(word in text for word in ORDER_TASK_KEYWORDS):
        score += 10
    if any(word in text for word in ["record", "records", "history", "detail", "details", "status", "balance"]):
        score += 4
    if any(word in text for word in ["terms", "privacy", "captcha", "logout", "delete", "payment", "服务条款", "隐私", "退出", "删除", "支付"]):
        score -= 12
    if element.get("isInViewport"):
        score += 1
    return score


def candidate_task_element_action(observation: dict, task: str, reason: str, message: str = "") -> dict | None:
    terms = task_terms(task)
    candidates = []
    for element in observation.get("elements", []) or []:
        score = element_action_score(element, terms)
        if score >= 5:
            candidates.append((score, element))
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        return None
    element = candidates[0][1]
    action = {"action": "click", "target_id": element["id"], "reason": reason}
    if message:
        action["message"] = message
    return action


def visible_link_click_for_url(observation: dict, url: str, reason: str, message: str = "") -> dict | None:
    target_url = normalize_candidate_href(url)
    if not is_http_url(target_url) or is_low_value_candidate(target_url):
        return None
    for element in observation.get("elements", []) or []:
        href = normalize_candidate_href(element.get("href", ""))
        if href == target_url and element.get("id"):
            action = {"action": "click", "target_id": element["id"], "reason": reason, "href": href}
            if message:
                action["message"] = message
            return action
    return None


def should_replan_direct_url_edit(action: dict, observation: dict, request: dict | None) -> bool:
    if not request or action.get("action") not in {"navigate", "open_tab"}:
        return False
    target_url = normalize_candidate_href(action.get("url", ""))
    current_url = normalize_url(observation.get("url", ""))
    if not is_http_url(target_url) or not is_http_url(current_url):
        return False
    if is_search_page(current_url) or is_search_page(target_url):
        return False
    if is_direct_resource_url(target_url):
        return False
    if target_url in str(request.get("task", "")):
        return False
    if host(target_url).lower() != host(current_url).lower():
        return False
    if normalize_url(target_url) == current_url:
        return False
    if visible_link_click_for_url(observation, target_url, "prefer_click_over_direct_url"):
        return False
    if not current_page_has_usable_context(observation):
        return False
    return True


def next_search_action(request: dict, reason: str, message: str) -> dict:
    visited = visited_urls(request.get("history", []), request.get("sources", []))
    current_url = normalize_url((request.get("observation") or {}).get("url", ""))
    current_host = host(current_url).lower()
    visited.add(current_url)
    templates = list(dict.fromkeys([*configured_search_engines(request), *SEARCH_ENGINES]))
    for template in templates:
        url = normalize_url(template.replace("{query}", quote(sanitize_search_query(request.get("task", "")))))
        if reason == "network_error" and current_host and host(url).lower() == current_host:
            continue
        if url not in visited:
            return {"action": "navigate", "url": url, "reason": reason, "message": message}
    return {
        "action": "done",
        "reason": reason,
        "answer": f"{message} 已经尝试了所有可用搜索源，当前没有找到可继续自动处理的可靠页面。",
    }


def query_from_search_url(url: str) -> str:
    try:
        parsed = urlparse(url or "")
        values = parse_qs(parsed.query)
        for key in ("q", "wd", "query", "keyword", "text", "search"):
            if values.get(key):
                return values[key][0] or ""
    except Exception:
        return ""
    return ""


def task_query_similarity(query: str, task: str) -> float:
    query_text = re.sub(r"\s+", " ", str(query or "")).strip().lower()
    task_text = re.sub(r"\s+", " ", str(task or "")).strip().lower()
    if not query_text or not task_text:
        return 0.0
    if query_text in task_text and len(query_text) > 80:
        return 1.0
    query_tokens = set(re.findall(r"[a-z0-9_.:-]{2,}|[\u4e00-\u9fff]{2,}", query_text))
    task_tokens = set(re.findall(r"[a-z0-9_.:-]{2,}|[\u4e00-\u9fff]{2,}", task_text))
    if not query_tokens:
        return 0.0
    return len(query_tokens & task_tokens) / len(query_tokens)


def query_tokens(query: str) -> list[str]:
    return re.findall(r"[a-z0-9_.:-]{2,}|[\u4e00-\u9fff]{2,}", str(query or "").lower())


def is_concise_search_query(query: str) -> bool:
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    if not text:
        return False
    lowered = text.lower()
    if (
        ("huggingface" in lowered or "hugging face" in lowered)
        and ("modelscope" in lowered or "魔搭" in lowered)
    ):
        return False
    if re.search(r"帮我|请|下载|优先|进行|尝试|分析", text):
        return False
    tokens = query_tokens(text)
    if len(text) <= 80 and 1 <= len(tokens) <= 4:
        return True
    return False


def search_query_contains_sensitive_text(query: str) -> bool:
    text = re.sub(r"\s+", " ", str(query or "")).strip()
    if not text:
        return False
    return bool(
        re.search(r"(密码|password|pass|pwd|验证码|账号|account|手机号|phone)\s*(?:是|为|=|:|：)?\s*\S+", text, flags=re.I)
        or re.search(r"\b\d{6,}\b", text)
    )


def search_query_needs_sanitizing(query: str, _task: str) -> bool:
    return search_query_contains_sensitive_text(query)


def rebuild_search_url(url: str, query: str) -> str:
    parsed = urlparse(url or "")
    hostname = parsed.hostname or ""
    if "baidu.com" in hostname:
        return f"https://www.baidu.com/s?wd={quote(query)}"
    if "duckduckgo.com" in hostname:
        return f"https://duckduckgo.com/?q={quote(query)}"
    if "sogou.com" in hostname:
        return f"https://www.sogou.com/web?query={quote(query)}"
    if "sm.cn" in hostname:
        return f"https://quark.sm.cn/s?q={quote(query)}"
    if "so.com" in hostname:
        return f"https://www.so.com/s?q={quote(query)}"
    return f"https://www.bing.com/search?q={quote(query)}"


def normalize_search_navigation(action: dict, request: dict | None) -> dict:
    if not request or action.get("action") not in {"navigate", "open_tab"}:
        return action
    if not is_search_page(action.get("url", "")):
        return action
    query = query_from_search_url(action.get("url", ""))
    if search_query_needs_sanitizing(query, request.get("task", "")):
        return replan_action("sensitive_search_query_replan", "The search URL appears to contain credentials or account data; replanning instead of navigating to it.")
    return action


def is_search_input_element(element: dict) -> bool:
    text = " ".join(
        str(element.get(key) or "")
        for key in (
            "type",
            "placeholder",
            "ariaLabel",
            "name",
            "label",
            "description",
            "actionHint",
            "nearbyText",
            "containerText",
            "formContext",
        )
    ).lower()
    return any(word in text for word in ["search", "搜索", "百度", "bing", "query", "wd"])


def search_input_action_for_url_edit(action: dict, observation: dict, request: dict | None) -> dict | None:
    if not request or action.get("action") not in {"navigate", "open_tab"}:
        return None
    target_url = normalize_candidate_href(action.get("url", ""))
    current_url = normalize_url((observation or {}).get("url", ""))
    if not is_http_url(target_url) or not is_http_url(current_url):
        return None
    if host(target_url).lower() != host(current_url).lower():
        return None
    query = query_from_search_url(target_url)
    if not query:
        return None
    if search_query_needs_sanitizing(query, request.get("task", "")):
        query = sanitize_search_query(request.get("task", ""))
    candidates = []
    for element in (observation or {}).get("elements", []) or []:
        if element.get("disabled") or not element.get("id"):
            continue
        tag = (element.get("tag") or "").lower()
        role = (element.get("role") or "").lower()
        element_type = (element.get("type") or "").lower()
        if tag not in {"input", "textarea"} and role not in {"searchbox", "textbox"} and element_type not in {"search", "text"}:
            continue
        if not is_search_input_element(element):
            continue
        score = 0
        if element.get("isInViewport"):
            score += 3
        if element_type == "search" or role == "searchbox":
            score += 3
        if "dataset" in element_text_blob(element).lower():
            score += 2
        candidates.append((score, element))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    target = candidates[0][1]
    return {
        "page_summary": action.get("page_summary", ""),
        "action": "type",
        "target_id": target["id"],
        "text": query,
        "reason": "prefer_search_input_over_url_edit",
        "message": "Converted a direct search URL edit into typing the same query into the visible site search box.",
    }


def normalize_search_typing(action: dict, observation: dict, request: dict | None) -> dict:
    if not request or action.get("action") != "type":
        return action
    text = action.get("text", "")
    element = {item.get("id"): item for item in observation.get("elements", [])}.get(action.get("target_id")) or {}
    if not is_search_input_element(element) and not is_search_page((observation or {}).get("url", "")):
        return action
    if search_query_contains_sensitive_text(text):
        return replan_action("sensitive_search_query_replan", "The search text appears to contain credentials or account data; replanning instead of typing it into search.")
    return action


def detect_blocking_page(request: dict) -> dict | None:
    observation = request.get("observation") or {}
    url = (observation.get("url") or "").lower()
    title = (observation.get("title") or "").lower()
    visible_text = (observation.get("visibleText") or "").strip()
    current_is_search_page = is_search_page(url)
    if task_explicitly_requests_download(request.get("task", "")) and is_direct_resource_url(observation.get("url", "")):
        return None
    if is_error_page(observation):
        return next_search_action(
            request,
            "network_error",
            "The page failed to load due to a network or server error. Trying another search engine.",
        )
    if is_login_or_account_page(observation) and not current_is_search_page and len(visible_text) < 1200:
        return None
    text = "\n".join([url, title, visible_text[:1200]]).lower()
    strong_url_or_title = any(
        keyword in f"{url}\n{title}"
        for keyword in ["captcha", "verify", "security", "sorry/index", "passport", "wappass", "access denied", "unusual traffic", "cloudflare"]
    )
    weak_text_block = (
        not current_is_search_page
        and len(visible_text) < 600
        and any(keyword.lower() in text for keyword in VERIFICATION_KEYWORDS)
    )
    if strong_url_or_title or weak_text_block:
        return next_search_action(request, "verification_or_login", "The current page appears blocked or needs verification.")
    if len(visible_text) < 30:
        return {"action": "wait", "ms": 1500, "reason": "unreadable_page"}
    return None


def validate_action(action: dict, observation: dict, request: dict | None = None) -> dict:
    if not isinstance(action, dict):
        return replan_action("invalid_model_action", "The model did not return a valid action object; replanning.")
    if action.get("action") not in ALLOWED_ACTIONS:
        return replan_action("unsupported_action_replan", f"Unsupported action: {action.get('action')}; replanning.")
    if action.get("action") == "ask_user" and request:
        return action
    action = normalize_search_navigation(action, request)
    model_first = model_first_strategy(request)

    if action.get("action") == "done" and request and task_explicitly_requests_download(request.get("task", "")):
        if has_verified_download(request):
            return action
        if not model_first:
            current_url = (observation or {}).get("url", "")
            if is_direct_resource_url(current_url):
                return {
                    "action": "download",
                    "url": current_url,
                    "filename": resource_filename_from_url(current_url),
                    "reason": "verify_download_before_done",
                    "message": "The current page is a direct data resource, but no local downloaded file has been verified yet.",
                }
            dataset_action = candidate_dataset_resource_action(observation, request, "verify_download_before_done")
            if dataset_action:
                return dataset_action
            if task_allows_saving_current_page(request.get("task", "")) and current_page_can_be_saved(observation):
                return {
                    "action": "save_page",
                    "filename": filename_from_task(request.get("task", "")),
                    "format": "text",
                    "reason": "save_current_page_before_done",
                    "message": "The current page contains the requested full text but no verified local file exists; saving current page text.",
                }
        return replan_action(
            "verify_download_before_done",
            "Download/save tasks are complete only after a successful download or save_page result and an existing non-empty local file. Decide whether to download a selected resource, save relevant page content, or continue inspecting candidates.",
        )

    elements = {element.get("id"): element for element in observation.get("elements", [])}
    if action["action"] in {"click", "type"}:
        element = elements.get(action.get("target_id"))
        if not element:
            if request and not model_first:
                dataset_fallback = candidate_dataset_resource_action(
                    observation,
                    request,
                    "stale_element_dataset_fallback",
                )
                if dataset_fallback:
                    return dataset_fallback
                fallback = candidate_click_action(
                    observation,
                    visited_urls(request.get("history", []), request.get("sources", [])),
                    request.get("task", ""),
                    "stale_element_fallback",
                    f"Target element disappeared: {action.get('target_id')}; clicking another visible candidate.",
                )
                if fallback:
                    return fallback
                task_element_fallback = candidate_task_element_action(
                    observation,
                    request.get("task", ""),
                    "stale_element_task_fallback",
                    f"Target element disappeared: {action.get('target_id')}; clicking a task-relevant visible element.",
                )
                if task_element_fallback:
                    return task_element_fallback
            if request and not model_first and is_login_or_account_page(observation) and not has_readable_business_context(observation):
                return {
                    "action": "wait",
                    "ms": 800,
                    "reason": "account_page_reobserve",
                    "message": "The login/account page changed after navigation; re-observing instead of pausing.",
                }
            return {
                "action": "wait",
                "ms": 500,
                "reason": "stale_element_reobserve",
                "message": f"Target element disappeared: {action.get('target_id')}",
            }
        if is_password_like_element(element) and not allow_password_input(request):
            return ask_user("Refusing to operate a password input.")
        if action["action"] == "click" and request and not model_first and is_login_or_account_page(observation):
            recent_same_target = recently_clicked_target(request, observation, action.get("target_id"), 4)
            if is_agreement_element(element) and (element.get("checked") is True or recent_same_target):
                submit_action = candidate_submit_action(observation, "agreement_already_checked_submit")
                if submit_action and submit_action.get("target_id") != action.get("target_id"):
                    return submit_action
                return replan_action("agreement_already_checked", "The agreement control is already checked; choose the submit/login button.")
            password_mode_action = candidate_password_mode_action(observation, request) if is_submit_element(element) else None
            if password_mode_action and password_mode_action.get("target_id") != action.get("target_id"):
                return password_mode_action
            agreement_action = None if recently_clicked_agreement(request, observation) else (
                candidate_agreement_action(observation) if is_submit_element(element) else None
            )
            if agreement_action and agreement_action.get("target_id") != action.get("target_id"):
                return agreement_action
        if element.get("href"):
            action["href"] = normalize_candidate_href(element["href"])
            if (
                not model_first
                and action["action"] == "click"
                and request
                and task_explicitly_requests_download(request.get("task", ""))
                and is_direct_download_href(action["href"], element_text_blob(element))
            ):
                return {
                    "action": "download",
                    "url": action["href"],
                    "filename": resource_filename_from_url(action["href"]),
                    "reason": "click_direct_download_as_download",
                    "message": "The clicked element is a direct download link; using download action.",
                }
        if action["action"] == "type":
            action = normalize_search_typing(action, observation, request)

    if action["action"] == "switch_tab":
        page_context = (request or {}).get("page_context") or {}
        pages = page_context.get("pages") or []
        page_id = str(action.get("page_id") or "").strip()
        selected_page = next((page for page in pages if page_id and page.get("pageId") == page_id), None)
        if selected_page is None and action.get("page_index") is not None:
            try:
                page_index = int(action.get("page_index"))
            except (TypeError, ValueError):
                page_index = -1
            selected_page = next((page for page in pages if page.get("index") == page_index), None)
            if selected_page:
                action["page_id"] = selected_page.get("pageId", "")
        if selected_page is None:
            return replan_action(
                "unknown_page_reobserve",
                "switch_tab must select a pageId from the currently observed page_context.pages.",
            )
        if selected_page.get("currentTask") or selected_page.get("active"):
            return replan_action(
                "page_already_selected",
                "The selected browser page is already the current task page; operate on it or select a different observed page.",
            )

    if not model_first and should_save_current_page_before_leaving(action, observation, request):
        return {
            "action": "save_page",
            "filename": filename_from_title(observation.get("title", ""), filename_from_task(request.get("task", ""), "saved_page.txt")),
            "format": "text",
            "reason": "save_current_page_before_leaving",
            "message": "The current page contains requested full text; saving it before leaving to search for additional materials.",
        }

    if action["action"] == "press" and action.get("key") != "Enter":
        return replan_action("unsupported_key_replan", "Only Enter is allowed; replanning.")
    if action["action"] in {"navigate", "open_tab"} and not is_http_url(action.get("url", "")):
        if request:
            return next_search_action(request, "invalid_url_replan", "Navigation only allows http/https URLs.")
        return replan_action("invalid_url_replan", "Navigation only allows http/https URLs; replanning.")
    if action["action"] in {"navigate", "open_tab"} and is_malformed_search_url(action.get("url", "")):
        return replan_action("malformed_search_url_replan", "The search URL is malformed; use a visible search box or a valid search URL.")
    if not model_first and action["action"] in {"navigate", "open_tab"} and not has_url_provenance(action, observation, request):
        return replan_action(
            "unverified_url_replan",
            "Direct navigation requires a URL from the user, a visible page link, a verified source/history URL, or a policy-generated search fallback.",
        )
    if action["action"] == "download":
        if not is_http_url(action.get("url", "")):
            return replan_action("invalid_download_url", "Download only allows http/https URLs.")
        if request and not task_explicitly_requests_download(request.get("task", "")):
            return ask_user("The model requested a download, but the user task did not explicitly ask to download or save a file.")
    if action["action"] == "save_page":
        if request and not task_explicitly_requests_download(request.get("task", "")):
            return ask_user("The model requested saving page content, but the user task did not explicitly ask to download or save content.")
        action["filename"] = action.get("filename") or "saved_page.txt"
    if (
        not model_first
        and action["action"] in {"navigate", "open_tab"}
        and request
        and is_search_page(action.get("url", ""))
        and task_refers_current_page(request.get("task", ""))
        and not task_explicitly_requests_search(request.get("task", ""))
        and current_page_has_usable_context(observation)
    ):
        return replan_action(
            "current_page_task_no_search",
            "The user asked to operate on the current page; stay on the observed page instead of searching those words.",
        )
    if action["action"] == "scroll":
        try:
            amount = int(float(action.get("amount", 800)))
        except Exception:
            amount = 800
        action["amount"] = max(min(amount, 3000), -3000)
    if action["action"] == "wait":
        try:
            ms = int(float(action.get("ms", 1000)))
        except Exception:
            ms = 1000
        action["ms"] = max(min(ms, 10000), 0)
    return action


def enforce_source_depth(action: dict, request: dict) -> dict:
    if action.get("action") != "done":
        return action
    task = request.get("task", "")
    if task_refers_current_page(task) and not task_explicitly_requests_search(task):
        return action
    if usable_sources(request.get("sources", [])):
        return action

    observation = request.get("observation") or {}
    scroll = observation.get("scroll") or {}
    current_url = normalize_url(observation.get("url", ""))
    scrolls_on_page = sum(
        1
        for item in request.get("history", []) or []
        if normalize_url(item.get("url", "")) == current_url and (item.get("action") or {}).get("action") == "scroll"
    )
    if scroll.get("canScrollDown") and scrolls_on_page < 1 and len((observation.get("viewportText") or "").strip()) > 120:
        return {
            "action": "scroll",
            "amount": max(700, int((scroll.get("viewportHeight") or 800) * 0.9)),
            "reason": "read_more_current_page",
            "message": "Need a non-search source page before final answer.",
        }

    dataset_action = candidate_dataset_resource_action(observation, request, "source_depth_dataset_resource")
    if dataset_action:
        return dataset_action

    visited = visited_urls(request.get("history", []), request.get("sources", []))
    click_action = candidate_click_action(observation, visited, request.get("task", ""), "need_real_source", "Opening a result page before final answer.")
    if click_action:
        return click_action
    task_element_action = candidate_task_element_action(
        observation,
        request.get("task", ""),
        "repeated_action_task_fallback",
        "Repeated action did not make progress; clicking a task-relevant visible element.",
    )
    if task_element_action:
        return task_element_action
    candidates = candidate_links(observation, visited, request.get("task", ""))
    if candidates:
        return {"action": "navigate", "url": candidates[0], "reason": "need_real_source", "message": "Opening a result page before final answer."}
    return next_search_action(request, "need_real_source", "Need a non-search source page before final answer.")


def enforce_page_scan(action: dict, request: dict) -> dict:
    return action


def prevent_repeated_action(action: dict, request: dict) -> dict:
    current_url = normalize_url((request.get("observation") or {}).get("url", ""))
    signature = ":".join([action.get("action", ""), action.get("target_id", ""), action.get("url", ""), action.get("key", "")])
    repeats = 0
    for item in (request.get("history") or [])[-8:]:
        item_action = item.get("action") or {}
        item_signature = ":".join(
            [item_action.get("action", ""), item_action.get("target_id", ""), item_action.get("url", ""), item_action.get("key", "")]
        )
        if normalize_url(item.get("url", "")) == current_url and item_signature == signature:
            repeats += 1

    action_name = action.get("action")
    last_result = request.get("last_result") or {}
    if action_name == "scroll":
        if last_result.get("message") != "scroll_no_progress" and repeats < 3:
            return action
    elif action_name in {"click", "type", "wait"}:
        if repeats < 2:
            return action
    elif action_name in {"navigate", "open_tab"}:
        if repeats < 1:
            return action
    else:
        return action

    observation = request.get("observation") or {}
    dataset_action = candidate_dataset_resource_action(observation, request, "repeated_dataset_resource_fallback")
    if dataset_action:
        return dataset_action

    if is_login_or_account_page(observation):
        password_mode_action = candidate_password_mode_action(observation, request, "repeated_password_mode_fallback")
        if password_mode_action:
            return password_mode_action
        agreement_action = None if recently_clicked_agreement(request, observation) else candidate_agreement_action(
            observation, "form_prerequisite_fallback"
        )
        if agreement_action:
            return agreement_action
        submit_action = candidate_submit_action(observation, "repeated_login_submit_fallback")
        if submit_action:
            return submit_action
        return ask_user("当前页面需要人工介入或确认。请处理页面后继续，智能体会重新观察当前页面。")

    visited = visited_urls(request.get("history", []), request.get("sources", []))
    click_action = candidate_click_action(
        request.get("observation") or {},
        visited,
        request.get("task", ""),
        "repeated_action_fallback",
        "Repeated action did not make progress; clicking another visible candidate.",
    )
    if click_action:
        return click_action
    candidates = candidate_links(request.get("observation") or {}, visited, request.get("task", ""))
    if candidates:
        return {"action": "navigate", "url": candidates[0], "reason": "repeated_action_fallback"}
    return next_search_action(request, "repeated_action_fallback", "Repeated action did not make progress.")

