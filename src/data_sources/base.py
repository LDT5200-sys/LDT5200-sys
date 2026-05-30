"""数据源基类。所有数据源 fetch() 返回 list[dict]，原始字段，未做归一化。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseDataSource(ABC):
    """可插拔数据源基类。

    子类只需实现 fetch()。归一化、去重等下游逻辑由 cleaner 统一处理。
    """

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))

    @abstractmethod
    def fetch(self) -> list[dict[str, Any]]:
        """返回原始记录列表，每条记录是一个 dict（列名→值）。"""

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name} enabled={self.enabled}>"
