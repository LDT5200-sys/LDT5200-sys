"""数据源工厂：根据 data_sources.yaml 实例化所有启用的源。"""
from __future__ import annotations

from typing import Any

from src.data_sources.base import BaseDataSource
from src.data_sources.browser_export_importer import BrowserExportImporter
from src.data_sources.csv_importer import CSVImporter
from src.data_sources.douyin_search_api import DouyinSearchAPI
from src.data_sources.douyin_search_source import DouyinSearchSource
from src.data_sources.excel_importer import ExcelImporter
from src.data_sources.json_importer import JSONImporter
from src.data_sources.public_search_adapter import PublicSearchAdapter
from src.data_sources.search_api_adapter import SearchAPIAdapter
from src.utils.config_loader import data_sources_config
from src.utils.logger import get_logger

logger = get_logger()

_REGISTRY = {
    "excel": ExcelImporter,
    "csv": CSVImporter,
    "json": JSONImporter,
    "search_api": SearchAPIAdapter,
    "public_search": PublicSearchAdapter,
    "douyin_search": DouyinSearchSource,
    "douyin_api": DouyinSearchAPI,
    "browser_export": BrowserExportImporter,
}

_KEYWORD_SOURCES = (SearchAPIAdapter, PublicSearchAdapter, DouyinSearchSource, DouyinSearchAPI)


def build_sources(keywords: list[str] | None = None) -> list[BaseDataSource]:
    cfg = data_sources_config()
    items = cfg.get("sources", []) or []
    sources: list[BaseDataSource] = []
    for item in items:
        name = item.get("name", "unnamed")
        type_ = item.get("type", "excel")
        cls = _REGISTRY.get(type_)
        if cls is None:
            logger.warning(f"未知的数据源类型 type={type_} name={name}，跳过")
            continue
        if cls in _KEYWORD_SOURCES:
            sources.append(cls(name, item, keywords or []))
        else:
            sources.append(cls(name, item))
    return sources


def fetch_all(
    keywords: list[str] | None = None,
    *,
    enable_overrides: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """按 yaml + 临时覆盖（如 --discover 强开 public_search）抓取所有数据源。"""
    overrides = enable_overrides or {}
    rows: list[dict[str, Any]] = []
    for src in build_sources(keywords):
        if src.name in overrides:
            src.enabled = bool(overrides[src.name])
        if not src.enabled:
            logger.info(f"数据源 {src.name} 未启用，跳过")
            continue
        try:
            data = src.fetch()
        except Exception as e:
            logger.error(f"数据源 {src.name} 抓取失败：{e}")
            continue
        logger.info(f"数据源 {src.name} 共 {len(data)} 条")
        rows.extend(data)
    return rows
