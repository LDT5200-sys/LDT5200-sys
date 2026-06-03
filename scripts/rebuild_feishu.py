"""重建飞书多维表：删除旧字段 → 创建正确类型字段 → 更新空记录 → 创建6份视图。"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from src.storage.sqlite_store import list_creators_with_status
from src.storage.excel_writer import _fmt_followers, _fmt_follower_tier, _fmt_contact
from src.utils.config_loader import load_env
from src.utils.logger import get_logger

logger = get_logger()

BASE = "https://open.feishu.cn/open-apis"
CST = timezone(timedelta(hours=8))
env = load_env()
APP_ID = env["FEISHU_APP_ID"]
APP_SECRET = env["FEISHU_APP_SECRET"]
APP_TOKEN = env["FEISHU_BITABLE_APP_TOKEN"]
TABLE_ID = env["FEISHU_BITABLE_TABLE_ID"]


def _auth():
    r = requests.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                      json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10)
    return r.json()["tenant_access_token"]


def _req(method, path, token, **kw):
    kw.setdefault("headers", {})
    kw["headers"]["Authorization"] = f"Bearer {token}"
    kw.setdefault("timeout", 60)
    r = requests.request(method, f"{BASE}{path}", **kw)
    if r.status_code == 204 or not r.text.strip():
        return {}
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"{method} {path}: {d.get('msg', d)}")
    return d


# ==================== 字段定义 ====================
# type: 1=多行文本, 3=单选, 5=日期, 15=超链接(URL)
FIELDS = [
    ("达人昵称", 1, None),
    ("抖音号", 1, None),
    ("粉丝数", 1, None),
    ("粉丝量分级", 3, ["不足1万", "1-5万", "5-10万", "10-50万", "50-100万", "100万+"]),
    ("内容类型", 3, ["测评", "穿搭", "生活记录", "口播", "剧情", "知识", "颜值", "其他"]),
    ("搜索关键词", 1, None),
    ("推荐等级", 3, ["S", "A", "B", "C", "淘汰"]),
    ("AI评分", 1, None),
    ("是否挂车", 3, ["是", "否", "未知"]),
    ("公开联系方式", 1, None),
    ("推荐理由", 1, None),
    ("风险点", 1, None),
    ("下一步动作", 3, ["优先联系", "持续观察", "暂不跟进", "淘汰"]),
    ("当前状态", 3, ["未查看", "合适", "不合适", "待联系", "已联系", "已报价", "已合作", "淘汰"]),
    ("达人主页链接", 15, None),  # type=15 超链接URL
    ("代表视频链接", 15, None),
    ("首次发现时间", 5, None),  # type=5 日期
    ("最近出现时间", 5, None),
    ("人工标签", 3, ["合适", "不合适", "待定", "需进一步了解"]),
    ("备注", 1, None),
]


def _field_body(name, ftype, options):
    if ftype == 3:  # 单选
        return {
            "field_name": name,
            "type": 3,
            "property": {"options": [{"name": o, "color": 0} for o in options]},
        }
    elif ftype == 15:  # 超链接URL
        return {"field_name": name, "type": 15, "ui_type": "Url"}
    elif ftype == 5:  # 日期
        return {"field_name": name, "type": 5}
    else:
        return {"field_name": name, "type": 1}


def _fmt_val(val):
    if val is None or val == "" or (isinstance(val, float) and val != val):
        return "-"
    if isinstance(val, float):
        return str(round(val, 1))
    return str(val)


def _to_date_ts(val):
    """将 'YYYY-MM-DD' 字符串转为 CST 时区的毫秒时间戳。"""
    if not val or val == "-":
        return None
    try:
        dt = datetime.strptime(str(val).strip(), "%Y-%m-%d")
        dt_cst = dt.replace(tzinfo=CST)
        return int(dt_cst.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _list_all_record_ids(token, table_id):
    """分页获取所有 record_id"""
    ids = []
    page_token = None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        resp = _req("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records",
                    token, params=params)
        items = resp.get("data", {}).get("items", [])
        for item in items:
            ids.append(item["record_id"])
        has_more = resp.get("data", {}).get("has_more", False)
        if not has_more:
            break
        page_token = resp.get("data", {}).get("page_token", "")
        if not page_token:
            break
    return ids


def main():
    token = _auth()
    logger.info("已获取飞书 token")

    table_id = TABLE_ID

    # ---- 1. 获取所有现有 record_id ----
    logger.info("获取现有记录 ID...")
    record_ids = _list_all_record_ids(token, table_id)
    logger.info(f"现有 {len(record_ids)} 条记录")

    # ---- 2. 删除所有非主字段，再重命名主字段 ----
    logger.info("获取现有字段...")
    f_resp = _req("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields",
                  token, params={"page_size": 50})
    existing_fields = f_resp.get("data", {}).get("items", [])
    logger.info(f"现有 {len(existing_fields)} 个字段")

    for f in existing_fields:
        if f.get("is_primary"):
            continue
        _req("DELETE", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields/{f['field_id']}",
             token)
        time.sleep(0.2)
    logger.info("非主字段已全部删除")

    for f in existing_fields:
        if f.get("is_primary"):
            logger.info(f"重命名主字段 {f['field_name']} → 达人昵称")
            _req("PUT", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields/{f['field_id']}",
                 token, json={"field_name": "达人昵称", "type": 1})
            time.sleep(0.2)
            break

    # ---- 3. 创建新字段 ----
    for name, ftype, options in FIELDS[1:]:
        body = _field_body(name, ftype, options)
        _req("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields",
             token, json=body)
        time.sleep(0.2)
    logger.info(f"已创建 {len(FIELDS) - 1} 个新字段（+1 个重命名的主字段）")

    # ---- 4. 构建数据 ----
    rows = list_creators_with_status()
    records = []
    for r in rows:
        records.append({
            "达人昵称": _fmt_val(r.get("creator_name")),
            "抖音号": _fmt_val(r.get("douyin_id")),
            "粉丝数": _fmt_val(_fmt_followers(r.get("latest_follower_count"))),
            "粉丝量分级": _fmt_val(_fmt_follower_tier(r.get("latest_follower_count"))),
            "内容类型": _fmt_val(r.get("main_content_type") or "-"),
            "搜索关键词": _fmt_val(r.get("search_keyword")),
            "推荐等级": _fmt_val(r.get("priority_level")),
            "AI评分": _fmt_val(r.get("latest_score")),
            "是否挂车": _fmt_val(r.get("has_product_link") or "未知"),
            "公开联系方式": _fmt_val(_fmt_contact(r.get("contact_text") or "")),
            "推荐理由": _fmt_val(r.get("recommend_reason")),
            "风险点": _fmt_val(r.get("risk_reason")),
            "下一步动作": _fmt_val(r.get("next_action") or "-"),
            "当前状态": _fmt_val(r.get("status")),
            "达人主页链接": r.get("creator_profile_url") or "",
            "代表视频链接": r.get("video_url") or "",
            "首次发现时间": _to_date_ts(r.get("first_seen_date")),
            "最近出现时间": _to_date_ts(r.get("last_seen_date")),
            "人工标签": "",
            "备注": "",
        })
    logger.info(f"构建 {len(records)} 条数据")

    # ---- 5. 分批更新现有空记录 ----
    total_data = min(len(records), len(record_ids))
    updated = 0
    for i in range(0, total_data, 500):
        batch_records = []
        for j in range(i, min(i + 500, total_data)):
            fields = dict(records[j])
            rid = record_ids[j]
            url = fields["达人主页链接"]
            if url and url != "-":
                fields["达人主页链接"] = {"link": url, "text": "🔗 打开主页"}
            else:
                fields["达人主页链接"] = ""
            url = fields["代表视频链接"]
            if url and url != "-":
                fields["代表视频链接"] = {"link": url, "text": "▶ 打开视频"}
            else:
                fields["代表视频链接"] = ""
            batch_records.append({"record_id": rid, "fields": fields})

        _req("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/batch_update",
             token, json={"records": batch_records})
        updated += len(batch_records)
        logger.info(f"已更新 {updated}/{total_data}")
        time.sleep(0.3)

    # ---- 6. 删除多余的空白记录 ----
    extra_ids = record_ids[total_data:]
    if extra_ids:
        logger.info(f"删除 {len(extra_ids)} 条多余空白记录...")
        for idx, rid in enumerate(extra_ids):
            _req("DELETE", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records/{rid}",
                 token)
            if (idx + 1) % 50 == 0:
                logger.info(f"  已删除 {idx + 1}/{len(extra_ids)}")
            time.sleep(0.15)
        logger.info("多余记录已删除")

    # ---- 7. 清理旧视图，创建 6 份新视图 ----
    logger.info("清理旧视图...")
    v_resp = _req("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views",
                  token, params={"page_size": 50})
    existing_views = v_resp.get("data", {}).get("items", [])
    for v in existing_views:
        if v["view_name"] == "表格":
            continue
        _req("DELETE", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views/{v['view_id']}",
             token)
        time.sleep(0.2)

    total = len(records)
    view_size = total // 6
    for i in range(6):
        start = i * view_size + 1
        end = start + view_size - 1 if i < 5 else total
        view_name = f"第{i+1}份（{start}-{end}行）"
        _req("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/views",
             token, json={"view_name": view_name, "view_type": "grid"})
        time.sleep(0.2)
        print(f"  视图已创建：{view_name}")

    print(f"\n✅ 重建完成！{total} 条数据，6 个视图")
    print(f"🔗 https://bytedance.feishu.cn/base/{APP_TOKEN}")


if __name__ == "__main__":
    main()
