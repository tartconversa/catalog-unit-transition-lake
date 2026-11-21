"""已交接报价的影响追溯与人工重判。

支持的追溯问题：
- 一条已认领/已交接的目录行受更正影响后，哪些批次仍能按原单位履约；
- 哪条新目录行会因此不能生效；
- 渠道是否已确认处置（认领/释放/拒绝），处置依据是重贴签、换货还是暂停接单。

人工圈定“某声明对某批次不适用”时，声明关系保留历史但标记 overridden：
批次实物量不变、可出货量按失去换算/整箱资格重算、标签状态按既有证书保留，
已拆派生批次同时重判。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .availability import (
    AvailabilityEngine,
    LINE_ACTIVE,
    LINE_CLAIMED,
    LINE_CLAIMED_AT_RISK,
    LINE_RELEASED,
    LINE_REJECTED,
    REASON_DIRECT,
    REASON_HOLD,
    REASON_IDENTICAL_CONVERSION,
    REASON_LABEL_MISSING,
    REASON_LOCKED,
    REASON_PARENT_DISQUALIFIED,
    REASON_UNPACK_ONLY,
    REASON_UNRESOLVED,
)
from .catalog import ClaimBook
from .inventory import (
    Inventory,
)
from .models import DISAPPLY_OVERRIDE, IngestedBatch, RECEIPT_RELEASED

# 处置建议
DISPOSITION_RELABEL = "relabel"      # 重贴签：存在 hold 证书，标签确认后可恢复
DISPOSITION_EXCHANGE = "exchange"    # 换货：原批次丧失资格，需以其他批次替换
DISPOSITION_PAUSE = "pause"          # 暂停接单：无可用替代且资格未恢复
DISPOSITION_OK = "ok"                # 履约能力未受影响


@dataclass(frozen=True)
class Override:
    """人工圈定：某声明对某批次自指定交接时点起不适用。"""

    claim: str
    lot: str
    reason: str
    effective_arrival: int
    decided_at: str


@dataclass(frozen=True)
class Impact:
    kind: str
    detail: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class LotFulfillment:
    lot: str
    package: str
    units_on_hand: int
    fulfillable_units: int
    label_state: str
    status: str          # fulfillable / locked / blocked / label_pending / unlabeled
    reasons: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class LineTrace:
    line: str
    channel: str
    sku: str
    unit: str
    requires_label: str
    receipt_timeline: tuple[dict, ...]
    fulfillment: tuple[LotFulfillment, ...]
    impacts: tuple[Impact, ...]
    disposition: str
    disposition_basis: tuple[str, ...]
    channel_decision: str | None
    blocked_successor_lines: tuple[str, ...]
    evidence: tuple[str, ...]


class OverrideBook:
    """人工不适用圈定的登记与持久化（独立于每日交接原件）。"""

    def __init__(self, lake_home: Path):
        self.path = Path(lake_home) / "overrides" / "claim_overrides.jsonl"
        self.overrides: list[Override] = []
        if self.path.exists():
            for row in self.path.read_text(encoding="utf-8").splitlines():
                if row.strip():
                    raw = json.loads(row)
                    self.overrides.append(Override(**raw))

    def disapply(self, claim: str, lot: str, reason: str,
                 effective_arrival: int) -> Override:
        for existing in self.overrides:
            if existing.claim == claim and existing.lot == lot:
                raise ValueError(f"声明 {claim} 对批次 {lot} 已存在不适用圈定")
        override = Override(
            claim=claim, lot=lot, reason=reason,
            effective_arrival=effective_arrival,
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(override.__dict__, ensure_ascii=False,
                                    sort_keys=True) + "\n")
        self.overrides.append(override)
        return override

    def pairs(self, as_of_arrival: int | None = None) -> set[tuple[str, str]]:
        """截至指定交接时点生效的 (声明, 批次) 圈定集合。"""
        return {(item.claim, item.lot) for item in self.overrides
                if as_of_arrival is None or item.effective_arrival <= as_of_arrival}

    def is_disapplied(self, claim: str, lot: str,
                      as_of_arrival: int | None = None) -> bool:
        return (claim, lot) in self.pairs(as_of_arrival)


def apply_overrides_to_claims(claims: ClaimBook,
                              overrides: set[tuple[str, str]]) -> ClaimBook:
    """返回带不适用标记的声明册视图（不修改原对象）。"""
    book = ClaimBook.__new__(ClaimBook)
    book._history = claims._history
    book.resolved = {}
    for key, resolved in claims.resolved.items():
        claim, _package, _child, lot = key
        if (claim, lot) in overrides:
            book.resolved[key] = type(resolved)(
                **{**resolved.__dict__, "scope": "unresolved",
                   "window_state": DISAPPLY_OVERRIDE})
        else:
            book.resolved[key] = resolved
    return book


class Tracer:
    """沿目录行回溯批次履约、更正影响与渠道处置确认。"""

    def __init__(self, batches: list[IngestedBatch], claims: ClaimBook,
                 inventory: Inventory, engine: AvailabilityEngine):
        self.batches = sorted(batches, key=lambda b: b.arrival)
        self.claims = claims
        self.inventory = inventory
        self.engine = engine
        self.evaluations = engine.evaluate()

    def trace(self, line_id: str) -> LineTrace:
        if line_id not in engine_line_ids(self.engine):
            raise KeyError(f"目录行不存在：{line_id}")
        line = self.engine.lines[line_id]
        receipt_history = sorted(self.engine.receipts_of.get(line_id, ()),
                                 key=lambda receipt: receipt.source_arrival)
        latest = receipt_history[-1] if receipt_history else None

        fulfillment = self._fulfillment(line_id)
        impacts = self._impacts(line_id, line, receipt_history)
        blocked_successors = self._blocked_successors(line_id, line)

        disposition, basis = self._disposition(
            line, latest, fulfillment, impacts, blocked_successors)

        evidence: list[str] = []
        for item in fulfillment:
            evidence.extend(item.evidence)
        for impact in impacts:
            evidence.extend(impact.evidence)
        return LineTrace(
            line=line_id, channel=line.channel, sku=line.sku, unit=line.unit,
            requires_label=line.requires_label,
            receipt_timeline=tuple(
                {"arrival": receipt.source_arrival, "state": receipt.state,
                 "publication": receipt.publication,
                 "allocation": [{"lot": lot, "units": qty}
                                for lot, qty in receipt.allocation]}
                for receipt in receipt_history),
            fulfillment=tuple(fulfillment), impacts=tuple(impacts),
            disposition=disposition, disposition_basis=tuple(dict.fromkeys(basis)),
            channel_decision=latest.state if latest else None,
            blocked_successor_lines=tuple(blocked_successors),
            evidence=tuple(dict.fromkeys(evidence)),
        )

    def _fulfillment(self, line_id: str) -> list[LotFulfillment]:
        evaluation = self.evaluations[line_id]
        line = evaluation.line
        locked_lots = {lock.lot: lock.units for lock in evaluation.locks}
        result: list[LotFulfillment] = []
        seen: set[tuple[str, str]] = set()
        for bucket in evaluation.supply:
            key = (bucket.lot, bucket.reason)
            if key in seen:
                continue
            seen.add(key)
            ledger = self.inventory.get(bucket.lot)
            reasons = [bucket.reason]
            if bucket.conversion is not None:
                reasons.append(f"claim={bucket.conversion.claim}:{bucket.conversion.scope}")
            if bucket.reason == REASON_LOCKED:
                status = "locked"
            elif bucket.reason in (REASON_DIRECT, REASON_IDENTICAL_CONVERSION) and bucket.sellable:
                status = "fulfillable"
            elif bucket.reason == REASON_HOLD:
                status = "label_pending"
            elif bucket.reason == REASON_PARENT_DISQUALIFIED:
                status = "claim_blocked"
            elif bucket.reason in (REASON_LABEL_MISSING, REASON_UNRESOLVED,
                                   REASON_UNPACK_ONLY):
                status = "blocked"
            else:
                status = "blocked"
            label_state = ("confirmed" if status == "fulfillable"
                           else "hold" if status == "label_pending"
                           else "missing" if bucket.label is None else bucket.label)
            result.append(LotFulfillment(
                lot=bucket.lot, package=bucket.package,
                units_on_hand=bucket.on_hand,
                fulfillable_units=(bucket.sellable if status == "fulfillable"
                                   else locked_lots.get(bucket.lot, 0)),
                label_state=label_state, status=status,
                reasons=tuple(reasons), evidence=bucket.evidence,
            ))
        return sorted(result, key=lambda item: item.lot)

    def _impacts(self, line_id: str, line, receipt_history) -> list[Impact]:
        impacts: list[Impact] = []
        # 声明按生产批次适用：只统计该行实际供给/锁定涉及的批次，
        # 其他批次（即使同包装同 SKU）的声明更正不株连本行。
        touched_lots = {bucket.lot for bucket in self.evaluations[line_id].supply}
        # 声明修订/不适用：
        # - 父批次整箱行：任一子组件关系 unresolved 即影响整箱资格
        # - 派生批次行：只看生成该批次的具体父->子关系，unpack 是合法解包，不算阻断
        for ledger in self.inventory.lots_in_package(line.unit):
            if ledger.lot not in touched_lots:
                continue
            if ledger.is_derived:
                # 已实拆派生批次按实物与贴签证书履约；来源关系 unresolved 只阻断
                # 未拆外箱的新拆售，不追溯否定已完成的翻包（unpack 同理合法）。
                # 仅在人工圈定声明对该父批次不适用时，才把派生批次列为受影响余货。
                edge = ledger.source_edge
                if edge is not None and edge.window_state == DISAPPLY_OVERRIDE:
                    impacts.append(Impact(
                        kind=DISAPPLY_OVERRIDE,
                        detail=f"派生批次 {ledger.lot} 的来源声明 {edge.claim} "
                               f"被人工圈定对 {ledger.origin} 不适用，"
                               f"余货 {ledger.lot} 需重判（实物量不变）",
                        evidence=tuple(f"units.csv#arrival{item.source_arrival}"
                                       for item in edge.history),
                    ))
                continue
            parent_overridden = any(edge.window_state == DISAPPLY_OVERRIDE
                                    for edge in ledger.applicable_edges)
            for edge in ledger.applicable_edges:
                if edge.uncertain:
                    kind = (DISAPPLY_OVERRIDE if edge.window_state == DISAPPLY_OVERRIDE
                            else "claim_unresolved")
                    word = "被人工圈定不适用" if kind == DISAPPLY_OVERRIDE else "被修订为不确定"
                    impacts.append(Impact(
                        kind=kind,
                        detail=f"批次 {ledger.lot} 的声明 {edge.claim} "
                               f"{edge.package}->{edge.child} {word}，"
                               f"整箱 {ledger.package} 报价受阻（实物量不变，可出货量重判）",
                        evidence=tuple(f"units.csv#arrival{item.source_arrival}"
                                       for item in edge.history),
                    ))
            # 仅人工圈定本父批次的声明不适用时，其已拆派生批次才一并重判；
            # 其他批次的圈定不株连本批次的派生货
            if parent_overridden:
                for child_lot in self._derived_lots(ledger.lot):
                    child_ledger = self.inventory.get(child_lot)
                    if child_ledger is None:
                        continue
                    impacts.append(Impact(
                        kind=DISAPPLY_OVERRIDE,
                        detail=f"已拆派生批次 {child_lot}（{child_ledger.package} "
                               f"x{child_ledger.on_hand_units}）随父批次 {ledger.lot} 重判："
                               f"实物量不变，标签仍按既有证书确认，未经新声明确认前"
                               f"不得按当前目录新增承诺",
                        evidence=child_ledger.evidence,
                    ))
        # 标签 hold
        for ledger in self.inventory.lots_in_package(line.unit):
            if ledger.lot not in touched_lots:
                continue
            hold = ledger.hold_for(line.requires_label)
            if hold:
                impacts.append(Impact(
                    kind="label_hold",
                    detail=f"批次 {ledger.lot} 有 {hold} 个 {line.requires_label} "
                           f"标签待确认（实物量不变）",
                    evidence=tuple(f"relabel:{cert.certificate}:hold"
                                   for cert in ledger.certificates
                                   if cert.action == "hold"),
                ))
        # 回执变化
        for receipt in receipt_history:
            if receipt.state == RECEIPT_RELEASED:
                impacts.append(Impact(
                    kind="receipt_released",
                    detail=f"渠道 {receipt.channel} 于 arrival {receipt.source_arrival} "
                           f"释放 {line_id} 的原锁定，释放明细："
                           + ",".join(f"{lot}:{qty}" for lot, qty in receipt.allocation),
                    evidence=(f"receipt.csv#arrival{receipt.source_arrival}",),
                ))
        return impacts

    def _derived_lots(self, parent_lot: str) -> list[str]:
        return [ledger.lot for ledger in self.inventory.all_lots()
                if ledger.origin == parent_lot]

    def _blocked_successors(self, line_id: str, line) -> list[str]:
        """同 SKU 的后续目录行中，因同一批声明/标签问题而不能生效的行。

        被渠道主动拒绝或已释放的行属于渠道决策，不算声明更正的后继受影响行。
        """
        source_line_arrival = max(item.source_arrival
                                  for item in self.engine.line_history[line_id])
        impacted_lots = {ledger.lot for ledger
                         in self.inventory.lots_in_package(line.unit)}
        blocked: list[str] = []
        for other_id, evaluation in self.evaluations.items():
            if other_id == line_id or evaluation.line.sku != line.sku:
                continue
            other_arrival = max(item.source_arrival
                                for item in self.engine.line_history[other_id])
            if other_arrival <= source_line_arrival:
                continue
            if evaluation.state in (LINE_ACTIVE, LINE_CLAIMED, LINE_CLAIMED_AT_RISK,
                                    LINE_REJECTED, LINE_RELEASED):
                continue
            touched_lots = {bucket.lot for bucket in evaluation.supply}
            if touched_lots & impacted_lots:
                blocked.append(other_id)
        return sorted(blocked)

    def _disposition(self, line, latest, fulfillment, impacts,
                     blocked_successors) -> tuple[str, list[str]]:
        basis: list[str] = []
        has_hold = any(impact.kind == "label_hold" for impact in impacts)
        has_claim_issue = any(impact.kind in ("claim_unresolved", DISAPPLY_OVERRIDE)
                              for impact in impacts)
        has_release = any(impact.kind == "receipt_released" for impact in impacts)
        fulfillable = sum(item.fulfillable_units for item in fulfillment
                          if item.status in ("fulfillable", "locked"))
        if has_release:
            basis.append("渠道已释放原锁定，库存须经新渠道回执确认后方可再写成可用")
            return DISPOSITION_PAUSE, basis
        if has_hold and not has_claim_issue:
            basis.append("存在 hold 贴签证书；实物量不变，标签确认后即可恢复出货")
            return DISPOSITION_RELABEL, basis
        if has_claim_issue:
            basis.append("适用声明被修订为不确定或被人工圈定不适用，原单位换算资格丧失")
            if fulfillable >= line.hold_units:
                basis.append(f"仍有 {fulfillable} 个已证实单位可按原单位履约，"
                             "其余实物量不得默认按当前目录出售")
                return DISPOSITION_OK, basis
            if blocked_successors:
                basis.append("因此不能生效的新目录行：" + ",".join(blocked_successors))
            basis.append(f"可履约 {fulfillable}/{line.hold_units}，缺口需以合规批次换货")
            return DISPOSITION_EXCHANGE, basis
        if fulfillable < line.hold_units:
            basis.append(f"可履约 {fulfillable}/{line.hold_units}，无其他合规来源，暂停接单")
            return DISPOSITION_PAUSE, basis
        return DISPOSITION_OK, basis


def engine_line_ids(engine: AvailabilityEngine) -> set[str]:
    return set(engine.lines)
