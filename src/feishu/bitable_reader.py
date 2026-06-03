"""飞书多维表读取：拉取已有达人列表，用于跨用户去重。"""
from __future__ import annotations

import time

import requests

from src.utils.config_loader import load_env
from src.utils.logger import get_logger

logger = get_logger()

BASE_URL = "https://open.feishu.cn/open-apis"


def _get_token(app_id: str, app_secret: str) -> str:
    resp = requests.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书 auth 失败：{data.get('msg', data)}")
    return data["tenant_access_token"]


def _list_tables(token: str, app_token: str) -> list[dict]:
    """列出多维表下的所有数据表。"""
    resp = requests.get(
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取表列表失败：{data.get('msg', data)}")
    return data.get("data", {}).get("items", [])


def _fetch_table_records(token: str, app_token: str, table_id: str) -> list[dict]:
    """分页拉取单张表的所有记录（仅拉 fields 字段）。"""
    all_records = []
    page_token = None
    page_size = 500

    while True:
        params = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token

        resp = requests.get(
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            logger.warning(f"读取表 {table_id} 记录失败：{data.get('msg', data)}")
            break

        items = data.get("data", {}).get("items", [])
        all_records.extend(items)

        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token")
        if not page_token:
            break
        time.sleep(0.15)

    return all_records


def fetch_known_creators(
    app_token: str | None = None,
    app_id: str | None = None,
    app_secret: str | None = None,
) -> tuple[set, set]:
    """从飞书多维表拉取所有已有的达人标识。

    返回 (known_urls: set, known_douyin_ids: set)，
    其中 known_urls 是达人主页链接集合，known_douyin_ids 是抖音号集合。
    用于流水线中过滤已发现的达人，避免多人重复筛选。

    如果飞书未配置或网络异常，返回空集合（不阻断流水线）。
    """
    try:
        env = load_env()
        app_id = app_id or env.get("FEISHU_APP_ID", "")
        app_secret = app_secret or env.get("FEISHU_APP_SECRET", "")
        app_token = app_token or env.get("FEISHU_BITABLE_APP_TOKEN", "")

        if not app_id or not app_secret or not app_token:
            logger.info("[飞书去重] 飞书未配置，跳过")
            return (set(), set())

        token = _get_token(app_id, app_secret)
        tables = _list_tables(token, app_token)
        if not tables:
            logger.info("[飞书去重] 没有找到数据表，跳过")
            return (set(), set())

        known_urls: set = set()
        known_douyin_ids: set = set()
        total_records = 0

        for t in tables:
            tid = t.get("table_id", "")
            tname = t.get("name", "")
            records = _fetch_table_records(token, app_token, tid)
            total_records += len(records)
            for r in records:
                fields = r.get("fields", {})

                # 达人主页链接 — 可能是 {"link": "...", "text": "..."} 或纯文本
                url = fields.get("达人主页链接", "")
                if isinstance(url, dict):
                    link = (url.get("link") or "").strip()
                    if link:
                        known_urls.add(link)
                elif isinstance(url, str) and url.strip():
                    known_urls.add(url.strip())

                # 抖音号
                douyin_id = fields.get("抖音号", "")
                if isinstance(douyin_id, str) and douyin_id.strip() and douyin_id.strip() != "-":
                    known_douyin_ids.add(douyin_id.strip())

            logger.info(f"[飞书去重] {tname} → {len(records)} 条记录")

        logger.info(
            f"[飞书去重] 总计 {total_records} 条，"
            f"去重标识 {len(known_urls)} 个链接 + {len(known_douyin_ids)} 个抖音号"
        )
        return (known_urls, known_douyin_ids)

    except Exception as e:
        logger.warning(f"[飞书去重] 拉取失败，跳过（不影响主流程）: {e}")
        return (set(), set())
