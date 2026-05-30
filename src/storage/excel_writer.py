"""Excel 报表写出：4 个 Sheet（Top 推荐 / 全部候选 / 关键词效果 / 疑似重复）。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.models.schemas import CreatorRecord, STANDARD_FIELDS
from src.utils.config_loader import field_mapping, DATA_DIR
from src.utils.logger import get_logger
from src.utils.time_utils import today_str

logger = get_logger()


_TOP_COLUMNS_CN = [
    "排名", "推荐等级", "AI评分", "达人昵称", "平台", "粉丝数", "内容类型",
    "是否接近果子模式", "推荐产品", "合作建议", "推荐理由", "风险点", "下一步动作",
    "达人主页链接", "代表视频链接", "链接类型",
    "公开联系方式", "联系方式类型", "联系方式位置",
    "提取状态", "缺失原因",
    "搜索关键词", "数据来源", "数据来源链接",
]


def _records_to_df(records: list[CreatorRecord]) -> pd.DataFrame:
    rows = [r.model_dump() for r in records]
    df = pd.DataFrame(rows)
    for col in STANDARD_FIELDS:
        if col not in df.columns:
            df[col] = ""
    df = df[STANDARD_FIELDS].fillna("")
    return df


def _to_chinese(df: pd.DataFrame) -> pd.DataFrame:
    headers = field_mapping().get("output_chinese_headers", {}) or {}
    return df.rename(columns={k: v for k, v in headers.items() if k in df.columns})


def _build_top(df: pd.DataFrame, top_n: int = 50) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=_TOP_COLUMNS_CN)
    ranked = df[df["priority_level"].isin(["S", "A", "B", "C"])].copy()
    ranked = ranked.sort_values("ai_score", ascending=False).head(top_n).fillna("")
    ranked.insert(0, "排名", range(1, len(ranked) + 1))
    ranked = ranked.rename(columns={
        "priority_level": "推荐等级",
        "ai_score": "AI评分",
        "creator_name": "达人昵称",
        "platform": "平台",
        "follower_count": "粉丝数",
        "content_type": "内容类型",
        "is_guozi_like": "是否接近果子模式",
        "recommended_product": "推荐产品",
        "cooperation_suggestion": "合作建议",
        "recommend_reason": "推荐理由",
        "risk_reason": "风险点",
        "next_action": "下一步动作",
        "creator_profile_url": "达人主页链接",
        "video_url": "代表视频链接",
        "url_type": "链接类型",
        "contact_text": "公开联系方式",
        "contact_type": "联系方式类型",
        "contact_location": "联系方式位置",
        "extraction_status": "提取状态",
        "missing_reason": "缺失原因",
        "search_keyword": "搜索关键词",
        "source_name": "数据来源",
        "source_url": "数据来源链接",
    })
    return ranked[_TOP_COLUMNS_CN]


def _build_keyword_stats(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["关键词", "候选数", "S级数", "A级数", "B级数", "平均AI评分"])
    work = df.copy()
    work["search_keyword"] = work["search_keyword"].fillna("(空)").replace("", "(空)")
    work["_is_S"] = (work["priority_level"] == "S").astype(int)
    work["_is_A"] = (work["priority_level"] == "A").astype(int)
    work["_is_B"] = (work["priority_level"] == "B").astype(int)
    g = work.groupby("search_keyword", as_index=False).agg(
        候选数=("priority_level", "size"),
        S级数=("_is_S", "sum"),
        A级数=("_is_A", "sum"),
        B级数=("_is_B", "sum"),
        平均AI评分=("ai_score", "mean"),
    )
    g["平均AI评分"] = g["平均AI评分"].round(1)
    g = g.rename(columns={"search_keyword": "关键词"})
    return g.sort_values("平均AI评分", ascending=False)


def write_daily_excel(
    records: list[CreatorRecord],
    suspects: list[CreatorRecord] | None = None,
    out_dir: Path | None = None,
) -> Path:
    out_dir = out_dir or (DATA_DIR / "output")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"daily_creator_result_{today_str()}.xlsx"

    df_all = _records_to_df(records)
    df_top = _build_top(df_all)
    df_kw = _build_keyword_stats(df_all)

    df_all_cn = _to_chinese(df_all)
    df_susp_cn = _to_chinese(_records_to_df(suspects or []))

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df_top.to_excel(writer, sheet_name="今日Top推荐", index=False)
        df_all_cn.to_excel(writer, sheet_name="全部候选", index=False)
        df_kw.to_excel(writer, sheet_name="关键词效果", index=False)
        df_susp_cn.to_excel(writer, sheet_name="疑似重复", index=False)
    logger.info(f"日报 Excel 写出：{path} 共 {len(df_all)} 条候选，Top {len(df_top)}")
    return path
