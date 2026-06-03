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
    "排名", "是否新达人", "推荐等级", "AI评分", "评分明细", "推荐理由",
    "达人昵称", "抖音号", "抖音达人类型", "粉丝数", "粉丝量分级", "量级",
    "推荐产品", "合作建议",
    "公开联系方式", "联系方式类型",
    "达人主页链接", "代表视频链接",
    "是否挂车", "赞粉比", "流量质量",
    "搜索关键词", "数据来源", "提取状态",
]


def _fmt_followers(v) -> str:
    """粉丝数格式化为万单位，如 105000 → 10.5w，小于1万显示原始数字"""
    if v is None or v == "" or (isinstance(v, float) and v != v):
        return ""
    try:
        n = int(float(v))
    except (ValueError, TypeError):
        return str(v)
    if n >= 10000:
        w = n / 10000
        return f"{w:.1f}w" if w < 100 else f"{int(w)}w"
    return str(n)


def _fmt_follower_tier(v) -> str:
    """粉丝量分级标签"""
    if v is None or v == "" or (isinstance(v, float) and v != v):
        return ""
    try:
        n = int(float(v))
    except (ValueError, TypeError):
        return ""
    if n < 10000:
        return "不足1万"
    elif n < 50000:
        return "1-5万"
    elif n < 100000:
        return "5-10万"
    elif n < 500000:
        return "10-50万"
    elif n < 1000000:
        return "50-100万"
    else:
        return "100万+"


def _records_to_df(records: list[CreatorRecord]) -> pd.DataFrame:
    rows = [r.model_dump() for r in records]
    df = pd.DataFrame(rows)
    for col in STANDARD_FIELDS:
        if col not in df.columns:
            df[col] = ""
    df = df[STANDARD_FIELDS].fillna("")
    # 粉丝量分级（必须在格式化为万单位之前，需要原始整数）
    if "follower_count" in df.columns:
        df["粉丝量分级"] = df["follower_count"].apply(_fmt_follower_tier)
    # 量级（头腰尾）
    if "follower_count" in df.columns:
        df["量级"] = df["follower_count"].apply(_fmt_creator_tier)
    # 赞粉比和流量质量（需要在粉丝数格式化之前）
    if "like_count" in df.columns and "follower_count" in df.columns:
        df["赞粉比"], df["流量质量"] = zip(*df.apply(
            lambda row: _fmt_like_ratio_and_flag(row["like_count"], row["follower_count"]),
            axis=1,
        ))
    else:
        df["赞粉比"] = "-"
        df["流量质量"] = "数据不足"
    # 粉丝数格式化为万单位
    if "follower_count" in df.columns:
        df["follower_count"] = df["follower_count"].apply(_fmt_followers)
    # 精简联系方式显示
    if "contact_text" in df.columns:
        df["contact_text"] = df["contact_text"].apply(_fmt_contact)
    if "contact_visible" in df.columns:
        df["contact_visible"] = df["contact_visible"].apply(
            lambda v: "有" if str(v) == "是" else ("无" if str(v) == "否" else "")
        )
    return df


def _fmt_contact(v) -> str:
    """精简联系方式文本"""
    if not v or v == "" or (isinstance(v, float) and v != v):
        return ""
    s = str(v)
    if s == "未发现公开联系方式":
        return "无"
    if "上游来源标注" in s:
        return s.replace("上游来源标注：", "").replace("（原文未提供）", "").strip()
    # 去掉长前缀
    s = s.replace("公开文本疑似联系方式 -> ", "")
    if len(s) > 50:
        s = s[:50] + "..."
    return s


def _fmt_creator_tier(v) -> str:
    """量级划分：头部/腰部/尾部"""
    if v is None or v == "" or (isinstance(v, float) and v != v):
        return "-"
    try:
        n = int(float(v))
    except (ValueError, TypeError):
        return "-"
    if n >= 1_000_000:
        return "头部"
    elif n >= 100_000:
        return "腰部"
    else:
        return "尾部"


def _fmt_like_ratio_and_flag(like_count, follower_count):
    """返回 (赞粉比文本, 流量质量标记)。"""
    try:
        likes = int(float(like_count))
        followers = int(float(follower_count))
    except (ValueError, TypeError):
        return ("-", "数据不足")
    if not likes or not followers:
        return ("-", "数据不足")

    ratio = likes / followers

    # 赞粉比格式化
    if ratio >= 1:
        ratio_text = f"{ratio:.1f}"
    elif ratio >= 0.01:
        ratio_text = f"{ratio * 100:.1f}%"
    else:
        ratio_text = f"{ratio * 100:.2f}%"

    # 流量质量判定
    if followers >= 1_000_000 and ratio < 0.001:
        flag = "疑似虚假流量"
    elif followers >= 100_000 and ratio < 0.005:
        flag = "疑似虚假流量"
    elif followers < 100_000 and ratio < 0.01:
        flag = "疑似虚假流量"
    elif ratio > 3.0:
        flag = "注意-高互动"
    else:
        flag = "正常"

    return (ratio_text, flag)


def _to_chinese(df: pd.DataFrame) -> pd.DataFrame:
    headers = field_mapping().get("output_chinese_headers", {}) or {}
    return df.rename(columns={k: v for k, v in headers.items() if k in df.columns})


def _get_new_creators(date: str) -> set:
    """从 SQLite 获取今天之前就存在的 creator_key 集合"""
    try:
        import sqlite3
        db = DATA_DIR / "database" / "creator_finder.db"
        if not db.exists():
            return set()
        conn = sqlite3.connect(str(db))
        rows = conn.execute(
            "SELECT creator_key FROM creators WHERE first_seen_date < ?", (date,)
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def _build_top(df: pd.DataFrame, top_n: int = 50) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=_TOP_COLUMNS_CN)
    ranked = df[df["priority_level"].isin(["S", "A", "B", "C"])].copy()
    ranked = ranked.sort_values("ai_score", ascending=False).head(top_n).fillna("")

    # 补充可能缺失的新字段
    for col in ["量级", "赞粉比", "流量质量"]:
        if col not in ranked.columns:
            ranked[col] = "-"

    # 标记是否新达人（用 creator_id 查 SQLite 历史）
    from src.utils.time_utils import today_str
    old_creators = _get_new_creators(today_str("%Y-%m-%d"))
    if "creator_id" in ranked.columns:
        ranked["是否新达人"] = ranked["creator_id"].apply(
            lambda cid: "🆕新" if (str(cid) and str(cid) not in old_creators) else "历史"
        )
    else:
        ranked["是否新达人"] = ""

    ranked.insert(0, "排名", range(1, len(ranked) + 1))
    ranked = ranked.rename(columns={
        "是否新达人": "是否新达人",
        "priority_level": "推荐等级",
        "ai_score": "AI评分",
        "risk_reason": "评分明细",
        "recommend_reason": "推荐理由",
        "creator_name": "达人昵称",
        "douyin_id": "抖音号",
        "creator_id": "达人ID",
        "follower_count": "粉丝数",
        "douyin_type": "抖音达人类型",
        "推荐产品": "推荐产品",
        "推荐产品": "推荐产品",
        "recommended_product": "推荐产品",
        "cooperation_suggestion": "合作建议",
        "creator_profile_url": "达人主页链接",
        "video_url": "代表视频链接",
        "has_product_link": "是否挂车",
        "url_type": "链接类型",
        "contact_text": "公开联系方式",
        "contact_type": "联系方式类型",
        "contact_location": "联系方式位置",
        "extraction_status": "提取状态",
        "missing_reason": "缺失原因",
        "search_keyword": "搜索关键词",
        "source_name": "数据来源",
        "source_url": "数据来源链接",
        "量级": "量级",
        "赞粉比": "赞粉比",
        "流量质量": "流量质量",
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
