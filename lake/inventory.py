"""批次库存台账。

在厂家声明册之上，把实收、翻包、占用与贴签证据合并成每个批次一份台账：
- 父批次：实物量 = 实收 - 已实拆（按 stock_moves 的 used 累计）；
- 派生批次：实物量即翻包记录数量，父子批次分开记账，杜绝重复计算；
- 标签量按证书拆分 confirmed / hold；无任何标签证据的量只作为
  “已证实实物量”保留，绝不默认按当前目录标签出售；
- 台账同时记录该批次适用的声明关系与整箱资格缺陷，供可售判定引用。
"""
from __future__ import annotations

from dataclasses import dataclass

from .catalog import ACTIVE, ClaimBook, ResolvedEdge
from .models import (
    DISAPPLY_OVERRIDE,
    IngestedBatch,
    MoveRecord,
    RelabelRecord,
    StockRecord,
)

# 台账层面的受阻原因码
BLOCK_LABEL_MISSING = "label_missing"                 # 批次标签暂缺
BLOCK_NO_APPLICABLE_CLAIM = "no_applicable_claim"     # 找不到适用的厂家声明
BLOCK_SUBCOMPONENT_UNRESOLVED = "subcomponent_unresolved"  # 外箱子类关系不确定
BLOCK_LABEL_PENDING = "label_pending"                 # 贴签 hold，标签待确认


@dataclass(frozen=True)
class LabelQty:
    """同一批次内按标签汇总的实物分量。"""

    label: str | None
    confirmed: int = 0
    hold: int = 0

    @property
    def total(self) -> int:
        return self.confirmed + self.hold


@dataclass(frozen=True)
class LotLedger:
    """一个批次当前的完整库存事实。"""

    lot: str
    package: str
    is_derived: bool
    origin: str | None
    produced_at: object
    source_arrival: int
    received_units: int
    opened_units: int
    on_hand_units: int
    label_breakdown: tuple[LabelQty, ...]
    certificates: tuple[RelabelRecord, ...]
    moves: tuple[MoveRecord, ...]
    applicable_edges: tuple[ResolvedEdge, ...]
    source_edge: ResolvedEdge | None
    unpack_edge: ResolvedEdge | None       # 翻包当时适用的来源关系
    block_reasons: tuple[str, ...]
    evidence: tuple[str, ...]

    @property
    def label_missing_units(self) -> int:
        return sum(item.total for item in self.label_breakdown if item.label is None)

    def confirmed_for(self, label: str) -> int:
        return sum(item.confirmed for item in self.label_breakdown if item.label == label)

    def hold_for(self, label: str) -> int:
        return sum(item.hold for item in self.label_breakdown if item.label == label)

    def confirmed_total(self) -> int:
        return sum(item.confirmed for item in self.label_breakdown if item.label is not None)

    def hold_total(self) -> int:
        return sum(item.hold for item in self.label_breakdown if item.label is not None)


class Inventory:
    """全部批次的台账集合。"""

    def __init__(self, batches: list[IngestedBatch], claims: ClaimBook):
        self._stock: dict[str, StockRecord] = {}
        self._moves_of: dict[str, list[MoveRecord]] = {}
        self._relabels_of: dict[str, list[RelabelRecord]] = {}
        self._children_of: dict[str, list[str]] = {}
        for batch in sorted(batches, key=lambda b: b.arrival):
            for rec in batch.stock:
                self._stock[rec.lot] = rec
            for move in batch.moves:
                self._moves_of.setdefault(move.origin, []).append(move)
                for child in move.children:
                    self._children_of.setdefault(move.origin, []).append(child)
            for rec in batch.relabels:
                self._relabels_of.setdefault(rec.lot, []).append(rec)
        self._claims = claims
        self.ledgers: dict[str, LotLedger] = {
            lot: self._build(lot) for lot in self._stock
        }

    def _label_breakdown(self, rec: StockRecord) -> tuple[LabelQty, ...]:
        certs = self._relabels_of.get(rec.lot, [])
        if not certs:
            # 没有翻包贴签证书：收货标签视为已证实；无标签则整批计入 label=None
            return (LabelQty(label=rec.label, confirmed=rec.units),)
        # 同一 (标签, 动作) 的后续证书是对该分量的最新圈定，按最新交接批次取值，
        # 不把历史证书累加（如 hold 8 之后重判为 confirmed 6 + hold 2）
        latest: dict[tuple[str | None, str], RelabelRecord] = {}
        for cert in sorted(certs, key=lambda item: item.source_arrival):
            latest[(cert.label, cert.action)] = cert
        totals: dict[str | None, dict[str, int]] = {}
        for (label, action), cert in latest.items():
            bucket = totals.setdefault(label, {"confirmed": 0, "hold": 0})
            bucket[action] = cert.scope_units
        accounted = sum(item["confirmed"] + item["hold"] for item in totals.values())
        breakdown = tuple(LabelQty(label=label, confirmed=qty["confirmed"], hold=qty["hold"])
                          for label, qty in sorted(totals.items(),
                                                   key=lambda kv: (kv[0] is None, kv[0] or "")))
        if accounted < rec.units:
            # 证书未覆盖的实物：标签暂缺，只给已证实量，不默认沿用目录标签
            missing = rec.units - accounted
            breakdown = breakdown + (LabelQty(label=None, confirmed=missing),)
        return breakdown

    def _build(self, lot: str) -> LotLedger:
        rec = self._stock[lot]
        moves = tuple(self._moves_of.get(lot, ()))
        opened = sum(move.used for move in moves)
        on_hand = 0 if rec.origin else rec.units - opened
        if rec.origin:
            on_hand = rec.units  # 派生批次按自身实物量独立记账
        certs = tuple(self._relabels_of.get(lot, ()))
        breakdown = self._label_breakdown(rec)

        source_edge: ResolvedEdge | None = None
        unpack_edge: ResolvedEdge | None = None
        applicable: list[ResolvedEdge] = []
        reasons: list[str] = []
        evidence = [f"{rec.source_file}#arrival{rec.source_arrival}:lot={lot}"]
        for move in moves:
            evidence.append(f"stock_moves:{move.move}")
        for cert in certs:
            evidence.append(f"relabel:{cert.certificate}:{cert.action}")

        if rec.origin is None:
            # 父批次：按实收日套用声明有效期；过期/未生效的声明不算适用，
            # 但人工圈定不适用（window_state 被标记为 claim_disapplied）必须保留
            for edge in self._claims.edges_for_lot(lot):
                resolved = self._claims.edge(edge.claim, edge.package, edge.child, lot,
                                             on_day=rec.at)
                if resolved is None:
                    continue
                if resolved.window_state in (ACTIVE, DISAPPLY_OVERRIDE):
                    applicable.append(resolved)
            if not applicable:
                reasons.append(BLOCK_NO_APPLICABLE_CLAIM)
            if any(edge.uncertain for edge in applicable):
                reasons.append(BLOCK_SUBCOMPONENT_UNRESOLVED)
        else:
            parent = self._stock.get(rec.origin)
            unpack_edge: ResolvedEdge | None = None
            if parent is not None:
                candidate = self._claims.find_conversion(rec.origin, parent.package,
                                                          rec.package)
                if candidate is not None:
                    # 翻包当时（按翻包记录的交接批次）适用的资格：证明已实拆货来源合法
                    unpack_edge = self._claims.edge_as_of_arrival(
                        candidate.claim, candidate.package, candidate.child,
                        rec.origin, rec.source_arrival)
                    current = self._claims.resolved.get(candidate.key)
                    if current is not None and current.window_state in (
                            ACTIVE, DISAPPLY_OVERRIDE):
                        source_edge = current
                        applicable = [current]
                evidence.append(f"stock:origin={rec.origin}")
            if source_edge is None:
                reasons.append(BLOCK_NO_APPLICABLE_CLAIM)

        if all(item.label is None for item in breakdown):
            reasons.append(BLOCK_LABEL_MISSING)
        if any(item.label is not None and item.hold for item in breakdown):
            reasons.append(BLOCK_LABEL_PENDING)

        return LotLedger(
            lot=lot, package=rec.package, is_derived=rec.origin is not None,
            origin=rec.origin, produced_at=rec.at, source_arrival=rec.source_arrival,
            received_units=rec.units, opened_units=opened, on_hand_units=on_hand,
            label_breakdown=breakdown, certificates=certs, moves=moves,
            applicable_edges=tuple(applicable), source_edge=source_edge,
            unpack_edge=unpack_edge,
            block_reasons=tuple(dict.fromkeys(reasons)), evidence=tuple(evidence),
        )

    def get(self, lot: str) -> LotLedger | None:
        return self.ledgers.get(lot)

    def all_lots(self) -> list[LotLedger]:
        return [self.ledgers[key] for key in sorted(self.ledgers)]

    def lots_in_package(self, package: str) -> list[LotLedger]:
        return [ledger for ledger in self.all_lots() if ledger.package == package]
