"""抖音数据源适配器（DouyinSearchSource）。

两种模式：
  模式 A（导入模式）：扫描 data/input/douyin/ 下的 Excel/CSV/HTML，按
    douyin_source.yaml 的专属字段映射归一化，输出标准 dict。
  模式 B（API 模式）：按 douyin_source.yaml 的 api_mode 配置调 REST 接口，
    返回结果按 field_mapping_overrides 做映射。

设计原则：
  - 不假设 AIDSO，不写死任何平台字段名，全走 yaml 映射。
  - 不绕过登录 / 验证码 / 风控。
  - 如果一条记录既没有主页链接也没有视频链接，标记 __missing_key_links__=true，
    下游评分和展示会据此降级。
  - API 失败不影响其他数据源。
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.data_sources.base import BaseDataSource
from src.utils.config_loader import ROOT, DATA_DIR, load_env, seed_keywords_config, field_mapping
from src.utils.logger import get_logger
from src.utils.time_utils import today_str

logger = get_logger()

# 抖音 html 导出文件里常见的 pattern：<a class="..." href="/user/...">昵称</a> 等
_RE_HREF_USER = re.compile(r'href="(/user/[A-Za-z0-9_\-]+)"')
_RE_HREF_VIDEO = re.compile(r'href="(/video/\d+)"')
_RE_TEXT = re.compile(r'>([^<]{1,200})<')


def _load_douyin_config() -> dict[str, Any]:
    path = ROOT / "config" / "douyin_source.yaml"
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class DouyinSearchSource(BaseDataSource):
    """抖音搜索数据源。"""

    def __init__(self, name: str, config: dict[str, Any], keywords: list[str] | None = None):
        super().__init__(name, config)
        self._dy_cfg = _load_douyin_config()
        self._keywords: list[str] = list(keywords or [])

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def fetch(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        rows: list[dict[str, Any]] = []

        # 模式 A：导入模式
        if self._dy_cfg.get("import_mode", {}).get("enabled", True):
            try:
                rows += self._fetch_import_mode()
            except Exception as e:
                logger.error(f"[{self.name}] 导入模式失败：{e}")

        # 模式 B：API 模式
        if self._dy_cfg.get("api_mode", {}).get("enabled", False):
            try:
                rows += self._fetch_api_mode()
            except Exception as e:
                logger.error(f"[{self.name}] API 模式失败：{e}")

        logger.info(f"[{self.name}] 总计 {len(rows)} 条")
        return rows

    # ------------------------------------------------------------------
    # 模式 A：本地导出表导入
    # ------------------------------------------------------------------

    def _fetch_import_mode(self) -> list[dict[str, Any]]:
        imp = self._dy_cfg.get("import_mode", {})
        rel_dir = imp.get("input_dir", "data/input/douyin").lstrip("/")
        input_dir = ROOT / rel_dir
        if not input_dir.exists():
            logger.info(f"[{self.name}] 导入目录不存在 {input_dir}，跳过")
            return []

        formats = imp.get("formats", ["xlsx", "csv", "html"])
        encoding = imp.get("encoding", "utf-8")

        all_rows: list[dict[str, Any]] = []
        for fmt in formats:
            pattern = f"*.{fmt}"
            for f in sorted(input_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
                if f.name.startswith("~$"):
                    continue
                try:
                    batch = self._read_file(f, fmt, encoding)
                    for rec in batch:
                        rec["__source_file__"] = f.name
                        rec["__source_name__"] = self.name
                        all_rows.append(rec)
                    logger.info(f"[{self.name}] 导入 {f.name} 共 {len(batch)} 行")
                except Exception as e:
                    logger.warning(f"[{self.name}] 读取 {f.name} 失败：{e}")

        return all_rows

    def _read_file(self, path: Path, fmt: str, encoding: str) -> list[dict[str, Any]]:
        if fmt in ("xlsx", "xls"):
            df = pd.read_excel(path, dtype=object)
            df = df.where(pd.notna(df), None)
            raw = df.to_dict(orient="records")
        elif fmt == "csv":
            df = None
            for enc in (encoding, "utf-8-sig", "gbk"):
                try:
                    df = pd.read_csv(path, dtype=object, encoding=enc)
                    break
                except UnicodeDecodeError:
                    continue
            if df is None:
                raise ValueError(f"无法用任何编码解析 CSV: {path}")
            df = df.where(pd.notna(df), None)
            raw = df.to_dict(orient="records")
        elif fmt == "html":
            raw = self._parse_html_export(path)
        else:
            raw = []
        return [self._map_fields(r) for r in raw]

    def _parse_html_export(self, path: Path) -> list[dict[str, Any]]:
        """抖音搜索页 HTML 导出：尽力提取链接和文字片段。

        不指望 100% 还原结构化数据——能在 HTML 里扒出几条算几条，
        其余字段在 normalizer 阶段标记 missing_reason。
        """
        html = path.read_text(encoding="utf-8", errors="replace")
        links: list[dict] = []

        # 按 <a> 标签拆分，提取链接和相邻文本
        for m_href in re.finditer(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.IGNORECASE):
            href = m_href.group(1)
            inner = re.sub(r"<[^>]+>", "", m_href.group(2)).strip()
            if not inner:
                continue
            link_type = "未知"
            full_url = href if href.startswith("http") else f"https://www.douyin.com{href}"
            if "/user/" in href:
                link_type = "主页"
                links.append({"主页链接": full_url, "达人昵称": inner, "链接类型": link_type})
            elif "/video/" in href:
                title = inner
                links.append({"视频链接": full_url, "视频标题": title, "链接类型": link_type})

        return links

    # ------------------------------------------------------------------
    # 模式 B：API 模式
    # ------------------------------------------------------------------

    def _fetch_api_mode(self) -> list[dict[str, Any]]:
        api = self._dy_cfg.get("api_mode", {})
        provider = api.get("provider", "generic")
        base_url = (api.get("base_url") or "").strip()
        api_key = (api.get("api_key") or "").strip() or load_env().get("DOUYIN_API_KEY", "")
        method = (api.get("method") or "GET").upper()
        timeout = int(api.get("timeout", 20))
        retries = int(api.get("retries", 2))
        params_template: dict = api.get("params_template", {}) or {}
        page_start = int(api.get("page_start", 1))
        page_end = int(api.get("page_end", 1))
        response_path: str = api.get("response_path", "")
        extra_headers: dict = api.get("headers", {}) or {}

        if not base_url:
            logger.warning(f"[{self.name}] API 模式已启用但 base_url 为空，跳过")
            return []
        if not self._keywords:
            logger.warning(f"[{self.name}] API 模式无可用关键词，跳过")
            return []

        rows: list[dict[str, Any]] = []
        for kw in self._keywords:
            for page in range(page_start, page_end + 1):
                params = deepcopy(params_template)
                for k, v in params.items():
                    if isinstance(v, str):
                        params[k] = v.format(keyword=kw, page=page)
                try:
                    payload = self._api_request(
                        base_url, method, params, api_key, extra_headers, timeout, retries, provider
                    )
                except Exception as e:
                    logger.warning(f"[{self.name}] API 请求失败 kw={kw} page={page}: {e}")
                    continue

                records = self._extract_response(payload, response_path)
                for rec in records:
                    if not isinstance(rec, dict):
                        continue
                    mapped = self._map_fields(rec)
                    mapped.setdefault("搜索关键词", kw)
                    mapped["__source_name__"] = self.name
                    rows.append(mapped)
                logger.info(f"[{self.name}] API kw={kw} page={page} → {len(records)} 条")
        return rows

    @staticmethod
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _api_request(
        base_url: str,
        method: str,
        params: dict,
        api_key: str,
        headers: dict,
        timeout: int,
        retries: int,
        provider: str,
    ) -> Any:
        hdrs = {**headers}
        if api_key:
            hdrs.setdefault("Authorization", f"Bearer {api_key}")
        if method == "GET":
            r = requests.get(base_url, params=params, headers=hdrs, timeout=timeout)
        elif method == "POST_JSON":
            r = requests.post(base_url, json=params, headers=hdrs, timeout=timeout)
        else:
            r = requests.request(method, base_url, json=params, headers=hdrs, timeout=timeout)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return {"raw_text": r.text}

    @staticmethod
    def _extract_response(payload: Any, path: str) -> list:
        if not path:
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                for k in ("data", "list", "items", "results"):
                    v = payload.get(k)
                    if isinstance(v, list):
                        return v
            return []
        node = payload
        for part in path.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                return []
            if node is None:
                return []
        return node if isinstance(node, list) else []

    # ------------------------------------------------------------------
    # 字段映射
    # ------------------------------------------------------------------

    def _map_fields(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把原始 dict 键名按专属映射 + 通用映射 映射到标准中文字段。"""
        mapped: dict[str, Any] = {"采集日期": today_str("%Y-%m-%d"), "数据来源": "douyin_search"}

        # 专属映射优先
        overrides = self._dy_cfg.get("field_mapping_overrides", {}) or {}
        for src_key, std_key in overrides.items():
            if not std_key:
                continue
            val = raw
            for part in src_key.split("."):
                if isinstance(val, dict):
                    val = val.get(part)
                elif isinstance(val, list) and part.isdigit():
                    idx = int(part)
                    val = val[idx] if idx < len(val) else None
                else:
                    val = None
                    break
            if val is not None:
                mapped[std_key] = val

        # 通用映射兜底（field_mapping.yaml 的 standard_fields）
        all_mappings = field_mapping()
        lookup: dict[str, str] = {}
        for std, aliases in (all_mappings.get("standard_fields") or {}).items():
            for a in (aliases or []):
                lookup[str(a).strip().lower()] = std
            lookup[std.lower()] = std

        for k, v in raw.items():
            if v is None or (isinstance(v, float) and v != v):  # NaN
                continue
            key_lower = str(k).strip().lower()
            std_key = lookup.get(key_lower)
            if std_key and std_key not in mapped:
                mapped[std_key] = str(v).strip() if not isinstance(v, (int, float)) else v

        # 联系方式未命中 → 不瞎编
        if not mapped.get("公开联系方式原文"):
            mapped["是否有联系方式"] = "未知"

        # 缺少关键链接的标记
        has_profile = bool(mapped.get("主页链接") or mapped.get("达人主页链接"))
        has_video = bool(mapped.get("视频链接") or mapped.get("代表视频链接"))
        mapped["__missing_key_links__"] = not has_profile and not has_video

        return mapped
