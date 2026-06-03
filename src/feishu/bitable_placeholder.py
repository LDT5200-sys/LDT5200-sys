"""飞书多维表：创建多维表 + 批量写入数据。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import requests

from src.utils.config_loader import DATA_DIR, load_env
from src.utils.logger import get_logger
from src.utils.time_utils import today_str

logger = get_logger()

BASE_URL = "https://open.feishu.cn/open-apis"


def _get_tenant_token(app_id: str, app_secret: str) -> str:
    resp = requests.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 token 失败：{data.get('msg', data)}")
    return data["tenant_access_token"]


def _create_app(token: str, name: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/bitable/v1/apps",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"name": name},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"创建多维表失败：{data.get('msg', data)}")
    return data["data"]["app"]


def _get_or_create_table(token: str, app_token: str, table_name: str) -> dict:
    # 先查已有的表
    resp = requests.get(
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") == 0:
        items = data.get("data", {}).get("items", [])
        if items:
            table = items[0]
            # 重命名第一个表
            if table.get("name") != table_name:
                requests.patch(
                    f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table['table_id']}",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"name": table_name},
                    timeout=10,
                )
            return {"table_id": table["table_id"], "name": table_name}

    # 没有表则创建
    resp = requests.post(
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"table": {"name": table_name}},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"添加数据表失败：{data.get('msg', data)}")
    t = data["data"]
    # API returns either {"table": {...}} or just {...}
    table = t.get("table", t)
    return {"table_id": table["table_id"], "name": table.get("name", table_name)}


def _add_fields(token: str, app_token: str, table_id: str, columns: list[str]) -> None:
    # 先查现有字段（默认只有一个「多行文本」字段）
    resp = requests.get(
        f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        headers={"Authorization": f"Bearer {token}"},
        params={"page_size": 50},
        timeout=10,
    )
    resp.raise_for_status()
    existing = {f["field_name"] for f in resp.json().get("data", {}).get("items", [])}

    for col in columns:
        if col in existing:
            continue
        body = {"field_name": col, "type": 1}  # type=1 即多行文本
        r = requests.post(
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning(f"添加字段 {col} 失败：{r.text[:200]}")
        else:
            existing.add(col)
        time.sleep(0.15)  # 避免频率限制


def _batch_insert(token: str, app_token: str, table_id: str, records: list[dict]) -> int:
    inserted = 0
    batch_size = 500  # 飞书限制每批最多 500 条
    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        resp = requests.post(
            f"{BASE_URL}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"records": batch},
            timeout=60,
        )
        if resp.status_code != 200:
            logger.warning(f"批次写入失败：{resp.text[:200]}")
            continue
        data = resp.json()
        if data.get("code") != 0:
            logger.warning(f"批次写入错误：{data.get('msg', data)}")
            continue
        inserted += len(batch)
        logger.info(f"已写入 {inserted}/{len(records)}")
        time.sleep(0.3)
    return inserted


def _records_payload(df: pd.DataFrame) -> list[dict]:
    return [
        {"fields": {
            k: ("" if pd.isna(v) else str(v))
            for k, v in row.items()
        }}
        for row in df.to_dict(orient="records")
    ]


def push_to_bitable(
    df: pd.DataFrame,
    app_token: str | None = None,
    table_id: str | None = None,
    dry_run: bool | None = None,
) -> dict | Path:
    """把 DataFrame 写入飞书多维表。

    如果 .env 中已配置 FEISHU_BITABLE_APP_TOKEN 和 FEISHU_BITABLE_TABLE_ID，
    则直接写入已有表；否则自动创建新的多维表，并把 token/table_id 回写到 .env。
    """
    env = load_env()
    app_id = env.get("FEISHU_APP_ID", "")
    app_secret = env.get("FEISHU_APP_SECRET", "")
    app_token = app_token or env.get("FEISHU_BITABLE_APP_TOKEN", "")
    table_id = table_id or env.get("FEISHU_BITABLE_TABLE_ID", "")

    if dry_run is None:
        dry_run = (not app_id) or (not app_secret)
    # 凭证缺失时强制 dry_run
    if not dry_run and ((not app_id) or (not app_secret)):
        logger.warning("[飞书] 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET，自动切换为 dry_run 模式")
        dry_run = True

    # dry_run 模式：只落本地 JSON
    if dry_run:
        out_dir = DATA_DIR / "processed"
        out_dir.mkdir(parents=True, exist_ok=True)
        fp = out_dir / f"bitable_payload_{today_str()}.json"
        payload = _records_payload(df)
        fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[飞书] dry_run 模式，本地 JSON：{fp}（{len(payload)} 条）")
        return {"status": "dry_run", "file": str(fp), "count": len(payload)}

    logger.info(f"[飞书] 开始推送 {len(df)} 条数据...")
    token = _get_tenant_token(app_id, app_secret)

    # 如果没有已有的表，则自动创建
    if not app_token or not table_id:
        app = _create_app(token, f"达人标注数据_{today_str()}")
        app_token = app["app_token"]
        logger.info(f"[飞书] 多维表已创建：{app['name']} ({app_token})")
        # 获取默认表
        table = _get_or_create_table(token, app_token, "待标注达人")
        table_id = table["table_id"]
        logger.info(f"[飞书] 数据表：{table['name']} ({table_id})")

    # 添加字段
    _add_fields(token, app_token, table_id, list(df.columns))

    # 写入数据
    payload = _records_payload(df)
    count = _batch_insert(token, app_token, table_id, payload)

    result = {
        "status": "ok",
        "app_token": app_token,
        "table_id": table_id,
        "app_url": f"https://bytedance.feishu.cn/base/{app_token}",
        "count": count,
    }
    logger.info(f"[飞书] 推送完成：{count} 条，{result['app_url']}")
    return result
