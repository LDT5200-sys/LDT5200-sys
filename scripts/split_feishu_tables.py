"""拆分飞书多维表：创建 6 张独立表，每人一张标注。"""
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


def _fmt_val(val):
    if val is None or val == "" or (isinstance(val, float) and val != val):
        return "-"
    if isinstance(val, float):
        return str(round(val, 1))
    return str(val)


def _to_date_ts(val):
    if not val or val == "-":
        return None
    try:
        dt = datetime.strptime(str(val).strip(), "%Y-%m-%d")
        dt_cst = dt.replace(tzinfo=CST)
        return int(dt_cst.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


FIELDS_SPEC = [
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
    ("达人主页链接", 15, None),
    ("代表视频链接", 15, None),
    ("首次发现时间", 5, None),
    ("最近出现时间", 5, None),
    ("人工标签", 3, ["合适", "不合适", "待定", "需进一步了解"]),
    ("备注", 1, None),
]


def _field_body(name, ftype, options):
    if ftype == 3:
        return {"field_name": name, "type": 3,
                "property": {"options": [{"name": o, "color": 0} for o in options]}}
    elif ftype == 15:
        return {"field_name": name, "type": 15, "ui_type": "Url"}
    elif ftype == 5:
        return {"field_name": name, "type": 5}
    else:
        return {"field_name": name, "type": 1}


def build_record(r):
    return {
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
    }


def prepare_batch(records_batch):
    cleaned = []
    for rec in records_batch:
        f = dict(rec)
        url = f["达人主页链接"]
        f["达人主页链接"] = {"link": url, "text": "🔗 打开主页"} if url and url != "-" else ""
        url = f["代表视频链接"]
        f["代表视频链接"] = {"link": url, "text": "▶ 打开视频"} if url and url != "-" else ""
        cleaned.append({"fields": f})
    return cleaned


def create_table_with_fields(token, table_name):
    """创建新表并建立所有字段。返回 table_id。"""
    resp = _req("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables",
                token, json={"table": {"name": table_name}})
    tid = resp["data"]["table_id"]
    time.sleep(0.3)

    # 处理默认字段：主字段重命名，其余删除
    f_resp = _req("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables/{tid}/fields",
                  token, params={"page_size": 50})
    for f in f_resp.get("data", {}).get("items", []):
        if f.get("is_primary"):
            _req("PUT", f"/bitable/v1/apps/{APP_TOKEN}/tables/{tid}/fields/{f['field_id']}",
                 token, json={"field_name": "达人昵称", "type": 1})
        else:
            _req("DELETE", f"/bitable/v1/apps/{APP_TOKEN}/tables/{tid}/fields/{f['field_id']}",
                 token)
        time.sleep(0.15)

    # 创建剩余字段
    for name, ftype, options in FIELDS_SPEC[1:]:
        _req("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{tid}/fields",
             token, json=_field_body(name, ftype, options))
        time.sleep(0.15)

    return tid


def insert_records(token, tid, records, label):
    """分批插入记录。"""
    total = len(records)
    for i in range(0, total, 500):
        batch = records[i:i + 500]
        _req("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{tid}/records/batch_create",
             token, json={"records": batch})
        time.sleep(0.3)
    logger.info(f"  {label}: {total} 条 ✓")


def main():
    token = _auth()
    logger.info("已获取飞书 token")

    # 加载数据
    rows = list_creators_with_status()
    all_records = [build_record(r) for r in rows]
    total = len(all_records)
    logger.info(f"共 {total} 条数据")

    # 拆成 6 份
    batch_size = total // 6
    batches = []
    for i in range(6):
        start = i * batch_size
        end = start + batch_size if i < 5 else total
        batches.append({
            "name": f"第{i+1}份（{start+1}-{end}行）",
            "records": all_records[start:end],
        })

    # ---- 第1份：写入现有表 ----
    logger.info(f"--- {batches[0]['name']} ---")
    prep = prepare_batch(batches[0]["records"])
    insert_records(token, TABLE_ID, prep, batches[0]["name"])
    logger.info(f"  🔗 https://bytedance.feishu.cn/base/{APP_TOKEN}?table={TABLE_ID}")

    # ---- 第2-6份：创建新表并写入 ----
    for b in batches[1:]:
        logger.info(f"--- {b['name']} ---")
        tid = create_table_with_fields(token, b["name"])
        time.sleep(0.3)
        prep = prepare_batch(b["records"])
        insert_records(token, tid, prep, b["name"])
        logger.info(f"  🔗 https://bytedance.feishu.cn/base/{APP_TOKEN}?table={tid}")

    # 汇总
    print(f"\n✅ 拆分完成！6 张表已就绪")
    print(f"🔗 https://bytedance.feishu.cn/base/{APP_TOKEN}")
    for b in batches:
        print(f"  {b['name']}: {len(b['records'])} 条")


if __name__ == "__main__":
    main()
