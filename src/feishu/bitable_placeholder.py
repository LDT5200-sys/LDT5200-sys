"""飞书多维表占位：第一版只把 payload 落到本地 JSON，方便后续接入。

接入方式：
1. 在 .env 填入 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BITABLE_APP_TOKEN /
   FEISHU_BITABLE_TABLE_ID
2. 实现 _call_feishu_api：拿 tenant_access_token，再调用 bitable records/batch_create
3. 把 PUSH_ENABLED 改为 True
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.utils.config_loader import DATA_DIR, load_env
from src.utils.logger import get_logger
from src.utils.time_utils import today_str

logger = get_logger()

PUSH_ENABLED = False  # 第一版默认不真正写入飞书


def _records_payload(df: pd.DataFrame) -> list[dict]:
    return [{"fields": {k: ("" if pd.isna(v) else v) for k, v in row.items()}}
            for row in df.to_dict(orient="records")]


def _call_feishu_api(payload: list[dict], app_token: str, table_id: str) -> dict:
    """真正调用飞书的实现（占位）。"""
    raise NotImplementedError(
        "第一版未实现真实写入。请在此处补全 tenant_access_token 获取与 batch_create_records 调用。"
    )


def push_to_bitable(
    df: pd.DataFrame,
    app_token: str | None = None,
    table_id: str | None = None,
    dry_run: bool | None = None,
) -> Path:
    """把 DataFrame 写入飞书多维表。第一版默认 dry_run=True，仅落本地 JSON。"""
    env = load_env()
    app_token = app_token or env["FEISHU_BITABLE_APP_TOKEN"]
    table_id = table_id or env["FEISHU_BITABLE_TABLE_ID"]
    if dry_run is None:
        dry_run = (not PUSH_ENABLED) or (not app_token) or (not table_id)

    out_dir = DATA_DIR / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / f"bitable_payload_{today_str()}.json"
    payload = _records_payload(df)
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if dry_run:
        logger.info(f"[飞书占位] dry_run 模式，仅落本地 {fp}（{len(payload)} 条）")
        return fp

    try:
        resp = _call_feishu_api(payload, app_token, table_id)
        logger.info(f"[飞书] 已推送 {len(payload)} 条，response={str(resp)[:200]}")
    except NotImplementedError as e:
        logger.warning(f"[飞书占位] {e}")
    except Exception as e:
        logger.error(f"[飞书] 推送失败：{e}")
    return fp
