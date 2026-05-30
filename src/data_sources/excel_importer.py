"""Excel 数据源：扫描 input_dir，按 pattern 读取所有匹配的 .xlsx/.xls 文件。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.data_sources.base import BaseDataSource
from src.utils.config_loader import ROOT
from src.utils.logger import get_logger

logger = get_logger()


class ExcelImporter(BaseDataSource):
    def fetch(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        input_dir = ROOT / self.config.get("input_dir", "data/input")
        pattern = self.config.get("pattern", "*.xlsx")
        pick_latest = bool(self.config.get("pick_latest", False))

        if not input_dir.exists():
            logger.info(f"[{self.name}] 目录不存在 {input_dir}，跳过")
            return []

        files = sorted(input_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        files = [f for f in files if not f.name.startswith("~$")]
        if not files:
            logger.info(f"[{self.name}] {input_dir} 下无匹配文件 {pattern}")
            return []

        if pick_latest:
            files = files[:1]

        rows: list[dict[str, Any]] = []
        for f in files:
            try:
                df = pd.read_excel(f, dtype=object)
            except Exception as e:
                logger.error(f"[{self.name}] 读取 {f.name} 失败：{e}")
                continue
            df = df.where(pd.notna(df), None)
            for rec in df.to_dict(orient="records"):
                rec["__source_file__"] = f.name
                rec["__source_name__"] = self.name
                rows.append(rec)
            logger.info(f"[{self.name}] 读取 {f.name} 共 {len(df)} 行")
        return rows
