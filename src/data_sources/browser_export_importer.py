"""浏览器导出数据源：仅处理用户主动导出的文件，不做任何抓取/绕登录。

实际上是 ExcelImporter / CSVImporter 的别名通道，单独保留是为了在 data_sources.yaml
里方便区分用途，并为以后浏览器扩展导出留口子。
"""
from __future__ import annotations

from typing import Any

from src.data_sources.base import BaseDataSource
from src.data_sources.csv_importer import CSVImporter
from src.data_sources.excel_importer import ExcelImporter
from src.data_sources.json_importer import JSONImporter


class BrowserExportImporter(BaseDataSource):
    def fetch(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        ftype = (self.config.get("file_type") or "excel").lower()
        delegate: BaseDataSource
        if ftype in ("xlsx", "xls", "excel"):
            delegate = ExcelImporter(self.name, self.config)
        elif ftype == "csv":
            delegate = CSVImporter(self.name, self.config)
        elif ftype == "json":
            delegate = JSONImporter(self.name, self.config)
        else:
            delegate = ExcelImporter(self.name, self.config)
        return delegate.fetch()
