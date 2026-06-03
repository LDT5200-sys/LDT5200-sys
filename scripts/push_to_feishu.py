"""一键推送累积数据到飞书多维表，分成 6 份分别给 6 人标注。"""
from __future__ import annotations

import sys
from pathlib import Path

# 确保项目根目录在 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.feishu.bitable_placeholder import push_to_bitable
from src.storage.sqlite_store import list_creators_with_status
from src.storage.excel_writer import _fmt_followers, _fmt_follower_tier, _fmt_contact
from src.utils.logger import get_logger

logger = get_logger()

COLUMNS = [
    "达人昵称", "抖音号", "粉丝数", "粉丝量分级", "量级",
    "内容类型", "搜索关键词", "推荐等级", "AI评分",
    "是否挂车", "赞粉比", "流量质量", "公开联系方式",
    "推荐理由", "风险点", "下一步动作",
    "当前状态", "达人主页链接", "代表视频链接",
    "人工标签", "备注",
]


def build_dataframe() -> pd.DataFrame:
    rows = list_creators_with_status()
    records = []
    for r in rows:
        records.append({
            "达人昵称": r.get("creator_name", ""),
            "抖音号": r.get("douyin_id") or "",
            "粉丝数": _fmt_followers(r.get("latest_follower_count")),
            "粉丝量分级": _fmt_follower_tier(r.get("latest_follower_count")),
            "量级": r.get("creator_tier", "-"),
            "内容类型": r.get("main_content_type", ""),
            "搜索关键词": r.get("search_keyword", ""),
            "推荐等级": r.get("priority_level", ""),
            "AI评分": str(round(r.get("latest_score", 0) or 0, 1)),
            "是否挂车": r.get("has_product_link") or "未知",
            "赞粉比": r.get("like_follower_ratio", "-"),
            "流量质量": r.get("traffic_quality_flag", "数据不足"),
            "公开联系方式": _fmt_contact(r.get("contact_text") or ""),
            "推荐理由": r.get("recommend_reason", ""),
            "风险点": r.get("risk_reason", ""),
            "下一步动作": r.get("next_action", ""),
            "当前状态": r.get("status", ""),
            "达人主页链接": r.get("creator_profile_url", ""),
            "代表视频链接": r.get("video_url", ""),
            "人工标签": "",
            "备注": "",
        })
    return pd.DataFrame(records)[COLUMNS]


def main():
    print("📊 从 SQLite 加载数据...")
    df = build_dataframe()
    print(f"   共 {len(df)} 条")

    print("🚀 推送到飞书多维表...")
    result = push_to_bitable(df, dry_run=False)

    if result.get("status") == "ok":
        print(f"\n✅ 推送成功！{result['count']} 条")
        print(f"🔗 打开多维表：{result['app_url']}")
        print(f"   app_token: {result['app_token']}")
        print(f"   table_id:  {result['table_id']}")
        print("\n💡 飞书里把数据按 6 份筛选视图分给标注人员即可。")
    else:
        print(f"\n⚠️  dry_run 模式：{result}")


if __name__ == "__main__":
    main()
