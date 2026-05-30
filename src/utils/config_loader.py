"""集中加载 yaml / .env 配置。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


# 硬编码默认值，保证云端和本地永远能读到
_DEFAULTS = {
    "SEARCH_API_PROVIDER": "bocha",
    "SEARCH_API_KEY": "sk-d6d73e45bfd748b0ba75f5111d44c803",
}


def _get_secret(key: str, default: str = "") -> str:
    """多层兜底：环境变量 → st.secrets → .env → 硬编码默认值"""
    # 1. 环境变量（最高优先级，用户显式设置）
    val = os.getenv(key)
    if val is not None and val != "":
        return str(val)
    # 2. Streamlit Cloud secrets
    try:
        import streamlit as st
        val = st.secrets.get(key) if hasattr(st.secrets, "get") else None
        if val is None:
            try:
                val = st.secrets[key]
            except (KeyError, TypeError):
                pass
        if val is not None and str(val) != "" and str(val).lower() != "false":
            return str(val)
    except Exception:
        pass
    # 3. .env 文件（由 python-dotenv 注入）
    # 4. 硬编码默认值
    if key in _DEFAULTS:
        return str(_DEFAULTS[key])
    return default


def load_env() -> dict:
    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    # 搜索 API：新名(SEARCH_API_*) 与旧名(SEARCH_*) 双写兼容
    search_provider = (
        _get_secret("SEARCH_API_PROVIDER")
        or _get_secret("SEARCH_PROVIDER")
    ).strip().lower()
    search_limit = int(
        _get_secret("SEARCH_API_RESULT_LIMIT")
        or _get_secret("SEARCH_RESULT_LIMIT")
        or "20"
    )

    return {
        # ---- LLM ----
        "OPENAI_API_KEY": _get_secret("OPENAI_API_KEY"),
        "OPENAI_BASE_URL": _get_secret("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "OPENAI_MODEL": _get_secret("OPENAI_MODEL", "gpt-4o-mini"),
        "KEYWORD_MODEL": _get_secret("KEYWORD_MODEL", "") or _get_secret("OPENAI_MODEL", "gpt-4o-mini"),
        "AI_TEMPERATURE": float(_get_secret("AI_TEMPERATURE", "0.2")),
        "AI_MAX_TOKENS": int(_get_secret("AI_MAX_TOKENS", "1200")),
        "AI_MAX_RETRIES": int(_get_secret("AI_MAX_RETRIES", "2")),
        "ENABLE_AI": _get_secret("ENABLE_AI", "true").lower() in ("1", "true", "yes"),

        # ---- 搜索 API（公开候选发现） ----
        "SEARCH_API_PROVIDER": search_provider,
        "SEARCH_API_KEY": _get_secret("SEARCH_API_KEY").strip(),
        "SEARCH_API_BASE_URL": _get_secret("SEARCH_API_BASE_URL").strip(),
        "SEARCH_API_RESULT_LIMIT": search_limit,
        "SEARCH_DOMAIN_FILTER": _get_secret(
            "SEARCH_DOMAIN_FILTER", "douyin.com,xingtu.cn,xiaohongshu.com"
        ).strip(),

        # 旧字段保留为别名
        "SEARCH_PROVIDER": search_provider,
        "SEARCH_RESULT_LIMIT": search_limit,

        # ---- 抖音/授权数据源 ----
        "DOUYIN_DATA_PROVIDER": _get_secret("DOUYIN_DATA_PROVIDER").strip().lower(),
        "DOUYIN_API_KEY": _get_secret("DOUYIN_API_KEY").strip(),
        "DOUYIN_API_SECRET": _get_secret("DOUYIN_API_SECRET").strip(),
        "DOUYIN_ACCESS_TOKEN": _get_secret("DOUYIN_ACCESS_TOKEN").strip(),
        "DOUYIN_API_BASE_URL": _get_secret("DOUYIN_API_BASE_URL").strip(),

        # ---- 星图数据源 ----
        "XINGTU_DATA_PROVIDER": _get_secret("XINGTU_DATA_PROVIDER").strip().lower(),
        "XINGTU_API_KEY": _get_secret("XINGTU_API_KEY").strip(),
        "XINGTU_API_SECRET": _get_secret("XINGTU_API_SECRET").strip(),
        "XINGTU_ACCESS_TOKEN": _get_secret("XINGTU_ACCESS_TOKEN").strip(),
        "XINGTU_API_BASE_URL": _get_secret("XINGTU_API_BASE_URL").strip(),

        # ---- 第三方达人数据工具 ----
        "THIRD_PARTY_PROVIDER": _get_secret("THIRD_PARTY_PROVIDER").strip().lower(),
        "THIRD_PARTY_API_KEY": _get_secret("THIRD_PARTY_API_KEY").strip(),
        "THIRD_PARTY_API_BASE_URL": _get_secret("THIRD_PARTY_API_BASE_URL").strip(),

        # ---- 飞书 ----
        "FEISHU_APP_ID": _get_secret("FEISHU_APP_ID"),
        "FEISHU_APP_SECRET": _get_secret("FEISHU_APP_SECRET"),
        "FEISHU_BITABLE_APP_TOKEN": _get_secret("FEISHU_BITABLE_APP_TOKEN"),
        "FEISHU_BITABLE_TABLE_ID": _get_secret("FEISHU_BITABLE_TABLE_ID"),
    }


def configured_status() -> dict[str, bool]:
    """返回各类外部依赖的"是否已配置"快照，给 UI / CLI 体检用。"""
    env = load_env()
    return {
        "OpenAI 大模型": bool(env["OPENAI_API_KEY"] and env["ENABLE_AI"]),
        "搜索 API": bool(env["SEARCH_API_PROVIDER"] and env["SEARCH_API_KEY"]),
        "抖音数据源": bool(env["DOUYIN_DATA_PROVIDER"] and (env["DOUYIN_API_KEY"] or env["DOUYIN_ACCESS_TOKEN"])),
        "星图数据源 (API)": bool(env["XINGTU_DATA_PROVIDER"] and (env["XINGTU_API_KEY"] or env["XINGTU_ACCESS_TOKEN"])),
        "星图数据源 (导出表)": (DATA_DIR / "input" / "xingtu").exists()
            and any((DATA_DIR / "input" / "xingtu").iterdir()),
        "第三方达人工具 (API)": bool(env["THIRD_PARTY_PROVIDER"] and env["THIRD_PARTY_API_KEY"]),
        "第三方达人工具 (导出表)": (DATA_DIR / "input" / "third_party").exists()
            and any((DATA_DIR / "input" / "third_party").iterdir()),
        "飞书多维表": bool(
            env["FEISHU_APP_ID"] and env["FEISHU_APP_SECRET"]
            and env["FEISHU_BITABLE_APP_TOKEN"] and env["FEISHU_BITABLE_TABLE_ID"]
        ),
    }


def _read_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def brand_profile() -> dict:
    return _read_yaml("brand_profile.yaml")


@lru_cache(maxsize=1)
def seed_keywords_config() -> dict:
    return _read_yaml("seed_keywords.yaml")


@lru_cache(maxsize=1)
def scoring_rules() -> dict:
    return _read_yaml("scoring_rules.yaml")


@lru_cache(maxsize=1)
def data_sources_config() -> dict:
    return _read_yaml("data_sources.yaml")


@lru_cache(maxsize=1)
def field_mapping() -> dict:
    return _read_yaml("field_mapping.yaml")
