"""SQLite 存储：creators / videos / daily_results / status_changes 四张表。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from src.models.schemas import CreatorRecord
from src.utils.config_loader import DATA_DIR
from src.utils.logger import get_logger
from src.utils.time_utils import today_str

logger = get_logger()

DB_PATH = DATA_DIR / "database" / "creator_finder.db"


_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS creators (
        creator_key TEXT PRIMARY KEY,
        platform TEXT,
        creator_name TEXT,
        creator_profile_url TEXT,
        latest_follower_count INTEGER,
        main_content_type TEXT,
        latest_score REAL,
        priority_level TEXT,
        first_seen_date TEXT,
        last_seen_date TEXT,
        status TEXT DEFAULT '未查看',
        contact_visible TEXT,
        contact_text TEXT,
        contact_type TEXT,
        contact_location TEXT,
        source_url TEXT,
        url_type TEXT,
        extraction_status TEXT,
        missing_reason TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS videos (
        video_key TEXT PRIMARY KEY,
        creator_key TEXT,
        video_url TEXT,
        video_title TEXT,
        publish_time TEXT,
        like_count INTEGER,
        comment_count INTEGER,
        share_count INTEGER,
        collect_date TEXT,
        ai_score REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_results (
        run_date TEXT,
        creator_key TEXT,
        video_key TEXT,
        source_name TEXT,
        search_keyword TEXT,
        ai_score REAL,
        priority_level TEXT,
        recommend_reason TEXT,
        risk_reason TEXT,
        next_action TEXT,
        contact_visible TEXT,
        contact_text TEXT,
        extraction_status TEXT,
        missing_reason TEXT,
        PRIMARY KEY (run_date, creator_key, video_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS status_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_key TEXT,
        old_status TEXT,
        new_status TEXT,
        changed_at TEXT,
        note TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_creators_status ON creators(status)",
    "CREATE INDEX IF NOT EXISTS idx_daily_run_date ON daily_results(run_date)",
]


# 用于向已有数据库做"软迁移"的列定义：(table, column, type)
_NEW_COLUMNS = [
    ("creators", "contact_visible", "TEXT"),
    ("creators", "contact_text", "TEXT"),
    ("creators", "contact_type", "TEXT"),
    ("creators", "contact_location", "TEXT"),
    ("creators", "source_url", "TEXT"),
    ("creators", "url_type", "TEXT"),
    ("creators", "extraction_status", "TEXT"),
    ("creators", "missing_reason", "TEXT"),
    ("daily_results", "contact_visible", "TEXT"),
    ("daily_results", "contact_text", "TEXT"),
    ("daily_results", "extraction_status", "TEXT"),
    ("daily_results", "missing_reason", "TEXT"),
]


def _ensure_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        # 软迁移：旧库补字段
        for table, column, ctype in _NEW_COLUMNS:
            try:
                cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")
            except sqlite3.Error as e:
                logger.warning(f"软迁移 {table}.{column} 失败：{e}")
        conn.commit()


@contextmanager
def get_conn():
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_eliminated_creator_keys() -> set[str]:
    """读取历史标记为淘汰的 creator_key 集合。"""
    with get_conn() as conn:
        try:
            rows = conn.execute(
                "SELECT creator_key FROM creators WHERE status = '淘汰' OR priority_level = '淘汰'"
            ).fetchall()
            return {r["creator_key"] for r in rows}
        except sqlite3.Error as e:
            logger.warning(f"读取淘汰名单失败：{e}")
            return set()


def upsert_records(records: Iterable[CreatorRecord], run_date: str | None = None) -> None:
    run_date = run_date or today_str("%Y-%m-%d")
    records = list(records)
    if not records:
        return

    with get_conn() as conn:
        cur = conn.cursor()
        for rec in records:
            ck = rec.creator_key()
            vk = rec.video_key()

            existing = cur.execute(
                "SELECT first_seen_date, status FROM creators WHERE creator_key=?", (ck,),
            ).fetchone()
            first_seen = existing["first_seen_date"] if existing else run_date
            keep_status = existing["status"] if existing else "未查看"

            cur.execute("""
                INSERT INTO creators (
                    creator_key, platform, creator_name, creator_profile_url,
                    latest_follower_count, main_content_type, latest_score,
                    priority_level, first_seen_date, last_seen_date, status,
                    contact_visible, contact_text, contact_type, contact_location,
                    source_url, url_type, extraction_status, missing_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(creator_key) DO UPDATE SET
                    platform=excluded.platform,
                    creator_name=excluded.creator_name,
                    creator_profile_url=excluded.creator_profile_url,
                    latest_follower_count=excluded.latest_follower_count,
                    main_content_type=excluded.main_content_type,
                    latest_score=excluded.latest_score,
                    priority_level=excluded.priority_level,
                    last_seen_date=excluded.last_seen_date,
                    contact_visible=excluded.contact_visible,
                    contact_text=excluded.contact_text,
                    contact_type=excluded.contact_type,
                    contact_location=excluded.contact_location,
                    source_url=excluded.source_url,
                    url_type=excluded.url_type,
                    extraction_status=excluded.extraction_status,
                    missing_reason=excluded.missing_reason
            """, (
                ck, rec.platform, rec.creator_name, rec.creator_profile_url,
                rec.follower_count, rec.content_type, rec.ai_score,
                rec.priority_level, first_seen, run_date, keep_status,
                rec.contact_visible, rec.contact_text, rec.contact_type, rec.contact_location,
                rec.source_url, rec.url_type, rec.extraction_status, rec.missing_reason,
            ))

            if vk:
                cur.execute("""
                    INSERT INTO videos (
                        video_key, creator_key, video_url, video_title, publish_time,
                        like_count, comment_count, share_count, collect_date, ai_score
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(video_key) DO UPDATE SET
                        like_count=excluded.like_count,
                        comment_count=excluded.comment_count,
                        share_count=excluded.share_count,
                        ai_score=excluded.ai_score,
                        collect_date=excluded.collect_date
                """, (
                    vk, ck, rec.video_url, rec.video_title, rec.publish_time,
                    rec.like_count, rec.comment_count, rec.share_count,
                    rec.collect_date or run_date, rec.ai_score,
                ))

            cur.execute("""
                INSERT OR REPLACE INTO daily_results (
                    run_date, creator_key, video_key, source_name, search_keyword,
                    ai_score, priority_level, recommend_reason, risk_reason, next_action,
                    contact_visible, contact_text, extraction_status, missing_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_date, ck, vk or "", rec.source_name, rec.search_keyword,
                rec.ai_score, rec.priority_level, rec.recommend_reason,
                rec.risk_reason, rec.next_action,
                rec.contact_visible, rec.contact_text, rec.extraction_status, rec.missing_reason,
            ))
        conn.commit()
    logger.info(f"SQLite 写入完成：{len(records)} 条 run_date={run_date}")


def update_creator_status(creator_key: str, new_status: str, note: str = "") -> None:
    from src.utils.time_utils import now_str
    with get_conn() as conn:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT status FROM creators WHERE creator_key=?", (creator_key,),
        ).fetchone()
        old = row["status"] if row else "未查看"
        cur.execute(
            "UPDATE creators SET status=? WHERE creator_key=?", (new_status, creator_key),
        )
        cur.execute("""
            INSERT INTO status_changes (creator_key, old_status, new_status, changed_at, note)
            VALUES (?, ?, ?, ?, ?)
        """, (creator_key, old, new_status, now_str(), note))
        conn.commit()
    logger.info(f"状态更新 {creator_key}: {old} → {new_status}")


def list_creators_with_status() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT creator_key, creator_name, platform, latest_follower_count, "
            "latest_score, priority_level, status, last_seen_date, "
            "contact_visible, contact_text, extraction_status, missing_reason, "
            "creator_profile_url, url_type "
            "FROM creators ORDER BY latest_score DESC"
        ).fetchall()
        return [dict(r) for r in rows]
