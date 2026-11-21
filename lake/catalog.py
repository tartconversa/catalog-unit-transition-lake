"""厂家包装声明册：修订合并、有效期判断与换算资格。

业务规则：
- 同一声明号对同一（父包装、子包装、批次）的后续行是修订；修订只调整
  明确给出的子包装，未提及的子包装继续沿用既有声明；
- 对照三态：identical 等价（可按子单位履约）、unpack 仅供解包换算、
  unresolved 不确定（依赖该关系的报价不得自动生效）；
- 声明按生产批次与出厂日适用，区间左闭右开、空结束表示持续有效；
- 外箱缺任一子组件资格时，整箱报价受阻，但独立贴签且仍被确认的其他
  子包装可继续按自己的目录行接单。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .models import (
    DISAPPLY_OVERRIDE,
    IngestedBatch,
    PackageEdge,
    SCOPE_IDENTICAL,
    SCOPE_UNRESOLVED,
)

ACTIVE = "active"          # 当前处于声明有效期内
EXPIRED = "expired"        # 有效期已过，但最新修订状态仍作为资格依据
NOT_YET = "not_yet"        # 尚未生效


@dataclass(frozen=True)
class ResolvedEdge:
    """合并修订后的一条有效包装关系及其历史。"""

    key: tuple[str, str, str, str]  # claim, package, child, lot
    claim: str
    package: str
    child: str
    lot: str
    factor: int
    scope: str
    window_state: str
    latest_arrival: int
    history: tuple[PackageEdge, ...]

    @property
    def sellable_conversion(self) -> bool:
        """是否允许按子包装单位直接对外履约（仅 identical 等价关系）。"""
        return self.scope == SCOPE_IDENTICAL

    @property
    def unpack_allowed(self) -> bool:
        """是否允许实际解包/翻包（identical 与 unpack 均可，unresolved 不可）。"""
        return self.scope != SCOPE_UNRESOLVED

    @property
    def uncertain(self) -> bool:
        return self.scope == SCOPE_UNRESOLVED


class ClaimBook:
    """全部交接批次合并后的声明视图。"""

    def __init__(self, batches: list[IngestedBatch]):
        self._history: dict[tuple[str, str, str, str], list[PackageEdge]] = {}
        for batch in sorted(batches, key=lambda b: b.arrival):
            for edge in batch.edges:
                key = (edge.claim, edge.package, edge.child, edge.lot)
                self._history.setdefault(key, []).append(edge)
        self.resolved: dict[tuple[str, str, str, str], ResolvedEdge] = {
            key: self._resolve(key, edges) for key, edges in self._history.items()
        }

    def _resolve(self, key: tuple[str, str, str, str],
                 edges: list[PackageEdge]) -> ResolvedEdge:
        latest = max(edges, key=lambda edge: edge.source_arrival)
        return ResolvedEdge(
            key=key, claim=latest.claim, package=latest.package, child=latest.child,
            lot=latest.lot, factor=latest.factor, scope=latest.scope,
            window_state=ACTIVE,  # 真实窗口状态在按批次查询时计算
            latest_arrival=latest.source_arrival, history=tuple(edges),
        )

    def edge(self, claim: str, package: str, child: str, lot: str,
             *, on_day: date | None = None) -> ResolvedEdge | None:
        """按声明键取合并后的关系，可附带指定日期的有效期状态。"""
        resolved = self.resolved.get((claim, package, child, lot))
        if resolved is None or on_day is None:
            return resolved
        # 人工圈定不适用是独立于有效期的状态，不得被日期重算覆盖
        if resolved.window_state == DISAPPLY_OVERRIDE:
            return resolved
        latest_history = max(resolved.history, key=lambda e: e.source_arrival)
        if on_day < latest_history.valid_from:
            window = NOT_YET
        elif latest_history.valid_until is not None and on_day >= latest_history.valid_until:
            window = EXPIRED
        else:
            window = ACTIVE
        return ResolvedEdge(**{**resolved.__dict__, "window_state": window})

    def edge_as_of_arrival(self, claim: str, package: str, child: str, lot: str,
                           arrival: int) -> ResolvedEdge | None:
        """取截至某交接批次时点生效的声明版本（用于还原翻包当时资格）。"""
        history = self._history.get((claim, package, child, lot))
        if not history:
            return None
        known = [edge for edge in history if edge.source_arrival <= arrival]
        if not known:
            return None
        latest = max(known, key=lambda edge: edge.source_arrival)
        current = self.resolved[(claim, package, child, lot)]
        return ResolvedEdge(**{**current.__dict__,
                               "factor": latest.factor, "scope": latest.scope,
                               "latest_arrival": latest.source_arrival,
                               "history": tuple(known)})

    def edges_for_lot(self, lot: str) -> list[ResolvedEdge]:
        """直接声明给某个生产批次的全部包装关系。"""
        return [edge for edge in self.resolved.values() if edge.lot == lot]

    def child_edges(self, lot: str, package: str) -> list[ResolvedEdge]:
        """某批次上某父包装的全部子包装关系（保留一个外箱多子类的事实）。"""
        return [edge for edge in self.edges_for_lot(lot) if edge.package == package]

    def parent_disqualifications(self, lot: str, package: str) -> list[ResolvedEdge]:
        """导致整箱报价不能自动继续的子组件关系（当前为 unresolved）。"""
        return [edge for edge in self.child_edges(lot, package) if edge.uncertain]

    def find_conversion(self, lot: str, package: str, child: str) -> ResolvedEdge | None:
        """在某批次上查找父包装到子包装的最新关系（不限声明号）。"""
        matches = [edge for edge in self.edges_for_lot(lot)
                   if edge.package == package and edge.child == child]
        if not matches:
            return None
        return max(matches, key=lambda edge: edge.latest_arrival)
