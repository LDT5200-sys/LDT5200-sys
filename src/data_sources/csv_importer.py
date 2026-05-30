"""CSV 数据源。"""
from __future__ import annotations

from typing import Any

import pandas as pd

from src.data_sources.base import BaseDataSource
from src.utils.config_loader import ROOT
from src.utils.logger import get_logger

logger = get_logger()


class CSVImporter(BaseDataSource):
    def fetch(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        input_dir = ROOT / self.config.get("input_dir", "data/input")
        pattern = self.config.get("pattern", "*.csv")
        encoding = self.config.get("encoding", "utf-8")
        pick_latest = bool(self.config.get("pick_latest", False))

        if not input_dir.exists():
            logger.info(f"[{self.name}] 目录不存在 {input_dir}，跳过")
            return []

        files = sorted(input_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            logger.info(f"[{self.name}] {input_dir} 下无匹配 CSV")
            return []
        if pick_latest:
            files = files[:1]

        rows: list[dict[str, Any]] = []
        for f in files:
            df = None
            for enc in (encoding, "utf-8-sig", "gbk"):
                try:
                    df = pd.read_csv(f, dtype=object, encoding=enc)
                    break
                except UnicodeDecodeError:
                    continue
                except Exception as e:
                    logger.error(f"[{self.name}] 读取 {f.name} 失败：{e}")
                    df = None
                    break
            if df is None:
                logger.error(f"[{self.name}] 无法解析 {f.name}（编码尝试失败）")
                continue
            df = df.where(pd.notna(df), None)
            for rec in df.to_dict(orient="records"):
                rec["__source_file__"] = f.name
                rec["__source_name__"] = self.name
                rows.append(rec)
            logger.info(f"[{self.name}] 读取 {f.name} 共 {len(df)} 行")
        return rows
