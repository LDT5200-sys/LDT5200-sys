"""公开搜索适配器：基于关键词 + site:douyin.com 通过搜索 API 拉公开网页结果。

支持 Provider:
- serper   (https://google.serper.dev/search)
- serpapi  (https://serpapi.com/search)
- bing     (https://api.bing.microsoft.com/v7.0/search)
- generic  (任意返回 JSON 的兼容接口；至少含 organic/items/webPages.value 之一)

设计原则：
- 不绕登录 / 不绕风控 / 不抓非公开页面
- 仅消费搜索引擎自身返回的公开摘要 + 链接
- 没有 SEARCH_API_KEY 时优雅降级，不抛异常
"""
from __future__ import annotations

from typing import Any, Iterable

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.data_sources.base import BaseDataSource
from src.utils.config_loader import load_env, seed_keywords_config
from src.utils.logger import get_logger
from src.utils.time_utils import today_str

logger = get_logger()

_DEFAULT_BASE_URL = {
    "serper": "https://google.serper.dev/search",
    "serpapi": "https://serpapi.com/search",
    "bing": "https://api.bing.microsoft.com/v7.0/search",
    "bocha": "https://api.bochaai.com/v1/web-search",
}

_NO_KEY_MSG = (
    "当前未配置搜索API（SEARCH_API_KEY 为空），无法自动发现公开网页结果。"
    "请配置 SEARCH_API_KEY，或先把导出的达人表放入 data/input/。"
)


class PublicSearchAdapter(BaseDataSource):
    """关键词 → 搜索引擎公开 API → 标准化候选记录。"""

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        keywords: list[str] | None = None,
    ):
        super().__init__(name, config)
        self.keywords: list[str] = [k for k in (keywords or []) if k]

    # ---------------------------- 主入口 ----------------------------

    def fetch(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        env = load_env()
        provider = (env.get("SEARCH_PROVIDER") or "").lower()
        api_key = env.get("SEARCH_API_KEY") or ""
        base_url = env.get("SEARCH_API_BASE_URL") or _DEFAULT_BASE_URL.get(provider, "")
        limit = int(env.get("SEARCH_RESULT_LIMIT") or 20)

        domain_filter = (
            self.config.get("site_filter")
            or env.get("SEARCH_DOMAIN_FILTER")
            or "douyin.com"
        )
        suffixes = self.config.get("extra_query_suffixes") or [""]

        # 关键词兜底：没传入就读 seed_keywords.yaml
        if not self.keywords:
            self.keywords = list(seed_keywords_config().get("seed_keywords", []) or [])

        if not api_key or not provider or not base_url:
            logger.warning(f"[{self.name}] {_NO_KEY_MSG}（provider={provider!r} base_url={base_url!r}）")
            return []
        if not self.keywords:
            logger.warning(f"[{self.name}] 没有可用关键词，跳过")
            return []

        rows: list[dict[str, Any]] = []
        seen_links: set[str] = set()
        per_kw_limit = max(1, limit // max(1, len(self.keywords) * len(suffixes)))

        for kw in self.keywords:
            for suffix in suffixes:
                full_kw = (kw + (" " + suffix if suffix else "")).strip()
                query = self._build_query(full_kw, domain_filter)
                try:
                    raw_results = self._search(provider, base_url, api_key, query, per_kw_limit)
                except Exception as e:
                    logger.warning(
                        f"[{self.name}] 搜索失败 provider={provider} kw={full_kw}: {e}"
                    )
                    continue

                for item in raw_results:
                    link = (item.get("link") or "").strip()
                    if not link or link in seen_links:
                        continue
                    seen_links.add(link)
                    rec = self._to_standard_record(item, full_kw, query, kw)
                    rec["__source_name__"] = self.name
                    rows.append(rec)
                logger.info(
                    f"[{self.name}] kw={full_kw} → {len(raw_results)} 条（累计 {len(rows)}）"
                )

        logger.info(f"[{self.name}] 完成，去重后 {len(rows)} 条公开搜索候选 ({today_str()})")
        return rows

    # ---------------------------- 查询构造 ----------------------------

    @staticmethod
    def _build_query(keyword: str, domain_filter: str) -> str:
        if not domain_filter:
            return keyword
        domains = [d.strip() for d in domain_filter.split(",") if d.strip()]
        if len(domains) == 1:
            return f"site:{domains[0]} {keyword}"
        site_clause = " OR ".join(f"site:{d}" for d in domains)
        return f"({site_clause}) {keyword}"

    # ---------------------------- HTTP 调用 ----------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _http(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        json_body: dict | None = None,
        timeout: int = 20,
    ) -> dict:
        r = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=timeout)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return {"raw_text": r.text}

    def _search(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        query: str,
        limit: int,
    ) -> list[dict]:
        if provider == "serper":
            data = self._http(
                "POST", base_url,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json_body={"q": query, "num": limit, "hl": "zh-cn", "gl": "cn"},
            )
            return self._normalize_serper(data)

        if provider == "serpapi":
            data = self._http(
                "GET", base_url,
                params={"q": query, "num": limit, "hl": "zh-cn", "api_key": api_key},
            )
            return self._normalize_serpapi(data)

        if provider == "bing":
            data = self._http(
                "GET", base_url,
                headers={"Ocp-Apim-Subscription-Key": api_key},
                params={"q": query, "count": limit, "mkt": "zh-CN"},
            )
            return self._normalize_bing(data)

        if provider == "bocha":
            # POST + JSON body + Bearer token
            data = self._http(
                "POST", base_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json_body={
                    "query": query,
                    "freshness": "noLimit",
                    "summary": True,
                    "count": min(limit, 50),
                },
            )
            return self._normalize_bocha(data)

        # generic：尽力兼容 organic/items/webPages.value
        data = self._http(
            "GET", base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            params={"q": query, "limit": limit},
        )
        return self._normalize_generic(data)

    # ---------------------------- 各 Provider 归一化 ----------------------------

    @staticmethod
    def _normalize_serper(data: dict) -> list[dict]:
        out = []
        for it in (data.get("organic") or []):
            out.append({
                "title": it.get("title", ""),
                "link": it.get("link", ""),
                "snippet": it.get("snippet", ""),
                "raw": it,
            })
        return out

    @staticmethod
    def _normalize_serpapi(data: dict) -> list[dict]:
        out = []
        for it in (data.get("organic_results") or []):
            out.append({
                "title": it.get("title", ""),
                "link": it.get("link", ""),
                "snippet": it.get("snippet") or it.get("description") or "",
                "raw": it,
            })
        return out

    @staticmethod
    def _normalize_bing(data: dict) -> list[dict]:
        out = []
        for it in ((data.get("webPages") or {}).get("value") or []):
            out.append({
                "title": it.get("name", ""),
                "link": it.get("url", ""),
                "snippet": it.get("snippet", ""),
                "raw": it,
            })
        return out

    @staticmethod
    def _normalize_bocha(data: dict) -> list[dict]:
        # Bocha 返回格式：{"data": {"webPages": {"value": [...]}}}
        inner = data.get("data", data)  # 兼容有/无 data 包裹
        web_pages = (
            (inner.get("webPages") or {}).get("value")
            or inner.get("webpages", {}).get("value")
            or []
        )
        out = []
        for it in web_pages:
            out.append({
                "title": it.get("name") or it.get("title", ""),
                "link": it.get("url") or it.get("link", ""),
                "snippet": it.get("snippet") or it.get("summary") or "",
                "raw": it,
            })
        return out

    @staticmethod
    def _normalize_generic(data: Any) -> list[dict]:
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = (
                data.get("organic")
                or data.get("organic_results")
                or data.get("items")
                or data.get("results")
                or ((data.get("webPages") or {}).get("value") if isinstance(data.get("webPages"), dict) else None)
                or []
            )
        else:
            items = []
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            out.append({
                "title": it.get("title") or it.get("name") or "",
                "link": it.get("link") or it.get("url") or "",
                "snippet": it.get("snippet") or it.get("description") or "",
                "raw": it,
            })
        return out

    # ---------------------------- 标准字段映射 ----------------------------

    @staticmethod
    def _to_standard_record(
        item: dict,
        full_keyword: str,
        full_query: str,
        seed_keyword: str,
    ) -> dict[str, Any]:
        link = item.get("link", "")
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        platform = "douyin" if "douyin.com" in link else (
            "xiaohongshu" if "xiaohongshu.com" in link else "other"
        )
        is_video = "/video/" in link
        is_user = "/user/" in link
        url_type = "视频" if is_video else ("主页" if is_user else "未知")
        rec: dict[str, Any] = {
            "采集日期": today_str("%Y-%m-%d"),
            "数据来源": "public_search",
            "数据来源链接": full_query,
            "平台": platform,
            "搜索关键词": seed_keyword,
            "视频标题": title if is_video else "",
            "视频文案": snippet if is_video else "",
            "账号简介": snippet if is_user else "",
            "原始文本": f"{title} | {snippet}".strip(" |"),
            "链接类型": url_type,
            "提取状态": "部分成功",
            "缺失原因": (
                "搜索结果只能拿到公开摘要，主页/视频详细字段需后续二次解析"
                if url_type != "未知"
                else "链接未识别为达人主页或视频，需人工确认"
            ),
            "raw_data": item.get("raw") or item,
        }
        if is_user:
            rec["主页链接"] = link
            # 搜索引擎给的 title 通常是 "昵称 - 平台" 或 "昵称｜简介"，取首段做昵称候选
            if title:
                head = title.split(" - ")[0].split("｜")[0].split("|")[0].strip()
                if head:
                    rec["达人昵称"] = head
        elif is_video:
            rec["视频链接"] = link
            # 推断主页：视频 url 的 user 信息搜索摘要里通常没给，留空，由 enricher 补
        else:
            rec["主页链接"] = ""
            rec["视频链接"] = ""
        return rec
