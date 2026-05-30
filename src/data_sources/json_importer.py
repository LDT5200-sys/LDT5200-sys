"""JSON 数据源。支持顶层数组、{"data": [...]}、{"list": [...]} 等常见结构。"""
from __future__ import annotations

import json
from typing import Any

from src.data_sources.base import BaseDataSource
from src.utils.config_loader import ROOT
from src.utils.logger import get_logger

logger = get_logger()


class JSONImporter(BaseDataSource):
    def fetch(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        input_dir = ROOT / self.config.get("input_dir", "data/input")
        pattern = self.config.get("pattern", "*.json")
        pick_latest = bool(self.config.get("pick_latest", False))

        if not input_dir.exists():
            logger.info(f"[{self.name}] 目录不存在 {input_dir}，跳过")
            return []

        files = sorted(input_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            logger.info(f"[{self.name}] {input_dir} 下无匹配 JSON")
            return []
        if pick_latest:
            files = files[:1]

        rows: list[dict[str, Any]] = []
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"[{self.name}] 解析 {f.name} 失败：{e}")
                continue

            records = self._extract_records(data)
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                rec["__source_file__"] = f.name
                rec["__source_name__"] = self.name
                rows.append(rec)
            logger.info(f"[{self.name}] 读取 {f.name} 共 {len(records)} 条")
        return rows

    @staticmethod
    def _extract_records(data: Any) -> list[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "list", "items", "result", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    return v
                if isinstance(v, dict):
                    for sub in ("list", "items"):
                        if isinstance(v.get(sub), list):
                            return v[sub]
            return [data]
        return []
