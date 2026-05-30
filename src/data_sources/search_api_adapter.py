"""通用搜索 API 适配器（占位，第一版默认 disabled）。

设计目标：
- 不写死任何具体平台
- 通过 yaml 配置 base_url、headers、params_template、分页范围、response_path
- 关键词来自外部传入（一般是 keyword_expander 产出的扩展词）

仅在用户主动配置 enabled=true 且填好 base_url 的情况下才会发请求。
不做任何爬取、不绕登录、不绕风控。
"""
from __future__ import annotations

from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from src.data_sources.base import BaseDataSource
from src.utils.logger import get_logger

logger = get_logger()


class SearchAPIAdapter(BaseDataSource):
    def __init__(self, name: str, config: dict[str, Any], keywords: list[str] | None = None):
        super().__init__(name, config)
        self.keywords = keywords or []

    def fetch(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        base_url = (self.config.get("base_url") or "").strip()
        if not base_url:
            logger.info(f"[{self.name}] 未配置 base_url，跳过")
            return []
        if not self.keywords:
            logger.info(f"[{self.name}] 没有传入关键词，跳过")
            return []

        method = (self.config.get("method") or "GET").upper()
        headers = self.config.get("headers") or {}
        timeout = int(self.config.get("timeout", 15))
        params_template: dict[str, Any] = self.config.get("params_template") or {}
        page_start = int(self.config.get("page_start", 1))
        page_end = int(self.config.get("page_end", 1))
        response_path = self.config.get("response_path", "")

        rows: list[dict[str, Any]] = []
        for kw in self.keywords:
            for page in range(page_start, page_end + 1):
                params = self._render_params(params_template, kw, page)
                try:
                    payload = self._request(base_url, method, params, headers, timeout)
                except Exception as e:
                    logger.warning(f"[{self.name}] 请求失败 keyword={kw} page={page}: {e}")
                    continue

                records = self._extract(payload, response_path)
                for rec in records:
                    if not isinstance(rec, dict):
                        continue
                    rec.setdefault("search_keyword", kw)
                    rec["__source_name__"] = self.name
                    rows.append(rec)
                logger.info(f"[{self.name}] keyword={kw} page={page} 返回 {len(records)} 条")
        return rows

    @staticmethod
    def _render_params(template: dict[str, Any], keyword: str, page: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in template.items():
            if isinstance(v, str):
                out[k] = v.format(keyword=keyword, page=page)
            else:
                out[k] = v
        return out

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8), reraise=True)
    def _request(self, url, method, params, headers, timeout):
        if method == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        else:
            r = requests.request(method, url, json=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return {"raw_text": r.text}

    @staticmethod
    def _extract(payload: Any, path: str) -> list:
        if not path:
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                for k in ("data", "list", "items"):
                    if isinstance(payload.get(k), list):
                        return payload[k]
            return []
        node = payload
        for part in path.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                return []
            if node is None:
                return []
        if isinstance(node, list):
            return node
        return []
