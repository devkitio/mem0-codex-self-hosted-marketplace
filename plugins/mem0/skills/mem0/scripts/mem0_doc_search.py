#!/usr/bin/env python3
"""按需检索 docs.mem0.ai，不在本地保存文档正文。

macOS/Linux 使用 `python3`，Windows 将其替换为 `python`：

    python3 mem0_doc_search.py --query "graph memory"
    python3 mem0_doc_search.py --page "/platform/features/graph-memory"
    python3 mem0_doc_search.py --index
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DOCS_BASE = "https://docs.mem0.ai"
LLMS_INDEX = f"{DOCS_BASE}/llms.txt"
MAX_RESPONSE_BYTES = 2_000_000
MAX_SEARCH_RESULTS = 20
MARKDOWN_DOC_LINK_RE = re.compile(
    r"\[([^\]\n]{1,200})\]\((https://docs\.mem0\.ai(?:/[^)\s]*)?)\)"
)

# 用于定向检索的已知文档区段
SECTION_MAP = {
    "platform": [
        "/platform/overview",
        "/platform/quickstart",
        "/platform/features",
        "/platform/features/graph-memory",
        "/platform/features/selective-memory",
        "/platform/features/custom-categories",
        "/platform/features/v2-memory-filters",
        "/platform/features/async-client",
        "/platform/features/webhooks",
        "/platform/features/multimodal-support",
    ],
    "api": [
        "/api-reference/memory/add-memories",
        "/api-reference/memory/v2-search-memories",
        "/api-reference/memory/v2-get-memories",
        "/api-reference/memory/get-memory",
        "/api-reference/memory/update-memory",
        "/api-reference/memory/delete-memory",
    ],
    "open-source": [
        "/open-source/overview",
        "/open-source/python-quickstart",
        "/open-source/node-quickstart",
        "/open-source/features",
        "/open-source/features/graph-memory",
        "/open-source/features/rest-api",
        "/open-source/configure-components",
    ],
    "sdks": [
        "/sdks/python",
        "/sdks/js",
    ],
    "integrations": [
        "/integrations",
    ],
}

SECTION_PREFIXES = {
    "platform": ("/platform/",),
    "api": ("/api-reference/",),
    "open-source": ("/open-source/", "/components/"),
    "sdks": ("/sdks/",),
    "integrations": ("/integrations",),
}

FETCH_ERROR_MESSAGES = {
    "响应内容超过大小限制",
    "URL 请求失败",
    "文档请求失败",
    "文档响应不是有效的 UTF-8 文本",
}


def is_allowed_docs_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "docs.mem0.ai"
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )
    except ValueError:
        return False


class DocsRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        if not is_allowed_docs_url(new_url):
            raise urllib.error.HTTPError(
                request.full_url,
                code,
                "拒绝重定向到非官方文档地址",
                headers,
                file_pointer,
            )
        return super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
        )


def open_docs_request(request):
    opener = urllib.request.build_opener(DocsRedirectHandler())
    return opener.open(request, timeout=15)


def fetch_url(url: str) -> str:
    """读取允许范围内的文档 URL。"""
    if not is_allowed_docs_url(url):
        return "只允许读取 https://docs.mem0.ai 下的文档页面"
    req = urllib.request.Request(url, headers={"User-Agent": "Mem0DocSearchAgent/1.0"})
    try:
        with open_docs_request(req) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return "响应内容超过大小限制"
            return raw.decode("utf-8")
    except urllib.error.HTTPError as error:
        return f"HTTP 错误 {error.code}"
    except urllib.error.URLError:
        return "URL 请求失败"
    except OSError:
        return "文档请求失败"
    except UnicodeDecodeError:
        return "文档响应不是有效的 UTF-8 文本"


def is_fetch_error(value: str) -> bool:
    """识别 fetch_url 返回的稳定错误，不把错误正文当作文档索引。"""
    return (
        value in FETCH_ERROR_MESSAGES
        or value.startswith("HTTP 错误 ")
        or value.startswith("只允许读取 ")
    )


def parse_index(content: str) -> list[dict[str, str]]:
    """从新版 llms.txt 中提取官方 Markdown 文档链接。"""
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in content.splitlines():
        for match in MARKDOWN_DOC_LINK_RE.finditer(line):
            title = match.group(1).strip()
            url = match.group(2)
            if not title or url in seen or not is_allowed_docs_url(url):
                continue
            entry = {"title": title, "url": url}
            description = line[match.end() :].strip().lstrip(":").strip()
            if description:
                entry["description"] = description
            entries.append(entry)
            seen.add(url)
    return entries


def matches_section(url: str, section: str | None) -> bool:
    if not section or section not in SECTION_PREFIXES:
        return True
    path = urllib.parse.urlsplit(url).path
    return any(path.startswith(prefix) for prefix in SECTION_PREFIXES[section])


def search_docs(query: str, section: str | None = None) -> dict:
    """检索 llms.txt 中经过校验的官方文档链接。"""
    index_content = fetch_url(LLMS_INDEX)
    if is_fetch_error(index_content):
        return {"source": "llms_txt_index", "query": query, "error": index_content}
    entries = parse_index(index_content)
    if not entries:
        return {
            "source": "llms_txt_index",
            "query": query,
            "error": "文档索引格式无效",
        }
    query_lower = query.casefold().strip()
    results = []
    for entry in entries:
        searchable = " ".join(entry.values()).casefold()
        if query_lower in searchable and matches_section(entry["url"], section):
            results.append(entry)
            if len(results) == MAX_SEARCH_RESULTS:
                break
    return {
        "source": "llms_txt_index",
        "query": query,
        "results": results,
        "matching_urls": [entry["url"] for entry in results],
        "suggestion": "可读取具体 URL 查看完整内容",
    }


def fetch_page(page_path: str) -> dict:
    """读取指定文档页面。"""
    url = f"{DOCS_BASE}{page_path}" if page_path.startswith("/") else page_path
    if not is_allowed_docs_url(url):
        return {"error": "只允许读取 https://docs.mem0.ai 下的文档页面"}
    content = fetch_url(url)
    if is_fetch_error(content):
        return {"url": url, "error": content}
    return {"url": url, "content": content[:10000], "truncated": len(content) > 10000}


def get_index() -> dict:
    """读取 llms.txt 中的完整文档索引。"""
    content = fetch_url(LLMS_INDEX)
    if is_fetch_error(content):
        return {"error": content}
    entries = parse_index(content)
    if not entries:
        return {"error": "文档索引格式无效"}
    urls = [entry["url"] for entry in entries]
    return {"total_pages": len(urls), "urls": urls, "sections": list(SECTION_MAP.keys())}


def list_section(section: str) -> dict:
    """列出指定文档区段中的已知页面。"""
    if section not in SECTION_MAP:
        return {"error": f"未知文档区段：{section}", "available": list(SECTION_MAP.keys())}
    return {
        "section": section,
        "pages": [f"{DOCS_BASE}{p}" for p in SECTION_MAP[section]],
    }


def configure_utf8_output() -> None:
    """覆盖 Windows 继承的旧控制台编码，保证中文输出稳定。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main():
    configure_utf8_output()
    parser = argparse.ArgumentParser(description="按需检索 Mem0 官方文档")
    parser.add_argument("--query", help="文档搜索词")
    parser.add_argument("--page", help="具体页面路径，例如 /platform/features/graph-memory")
    parser.add_argument("--index", action="store_true", help="显示完整文档索引")
    parser.add_argument("--section", help="按区段过滤或列出区段页面")
    parser.add_argument("--json", action="store_true", help="输出 JSON")

    args = parser.parse_args()

    if args.index:
        result = get_index()
    elif args.section and not args.query:
        result = list_section(args.section)
    elif args.page:
        result = fetch_page(args.page)
    elif args.query:
        result = search_docs(args.query, section=args.section)
    else:
        parser.print_help()
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if isinstance(result, dict):
            if "error" in result:
                print(f"错误：{result['error']}")
                if result.get("available"):
                    print(f"可用区段：{', '.join(result['available'])}")
            elif "results" in result:
                print(f"来源：{result.get('source', '未知')}")
                if result.get("query"):
                    print(f"查询：{result['query']}")
                if not result["results"]:
                    print("未找到匹配的官方文档。")
                for r in result["results"]:
                    print(f"  - {r.get('title', '无标题')}: {r.get('url', '无地址')}")
                    if r.get("description"):
                        print(f"    {r['description'][:200]}")
                if result.get("suggestion"):
                    print(f"\n{result['suggestion']}")
            elif "urls" in result:
                print(f"文档页面总数：{result['total_pages']}")
                print(f"区段：{', '.join(result['sections'])}")
                for url in result["urls"][:30]:
                    print(f"  - {url}")
                if result["total_pages"] > 30:
                    print(f"  ... 另有 {result['total_pages'] - 30} 个页面")
            elif "pages" in result:
                print(f"区段：{result['section']}")
                for page in result["pages"]:
                    print(f"  - {page}")
            elif "content" in result:
                print(f"URL：{result['url']}")
                if result.get("truncated"):
                    print("[内容已截断为 10000 个字符]")
                print(result["content"])
            else:
                print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
