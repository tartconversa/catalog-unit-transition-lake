"""目录行可售判定与渠道顺序占用。

两层库存概念必须分开：
1. locked（已锁定）：以 receipt 回执的 allocation 明细为准的真实承诺，
   claimed 仍在生效才计入；released 之后的库存不得被任何渠道写成可用；
2. committable（可承诺）：在锁定之外，按渠道 rank（数字小者优先，同级
   按渠道号）登记顺序贪心占用的建议承诺。

占用互斥：父批次（整箱）与翻包派生批次（盒）分池记账；只有 identical
等价关系允许把父包装折算进子包装供给，unpack 只解包不拆售，unresolved
则一律阻断。标签只认 confirmed 证据，hold 与无标签量只作已证实实物量。
"""
from __future__ import annotations

from dataclasses import dataclass

from .catalog import ClaimBook, ResolvedEdge
from .inventory import (
    BLOCK_LABEL_MISSING,
    BLOCK_LABEL_PENDING,
    BLOCK_NO_APPLICABLE_CLAIM,
    BLOCK_SUBCOMPONENT_UNRESOLVED,
    Inventory,
    LotLedger,
)
from .models import (
    ChannelLine,
    IngestedBatch,
    RECEIPT_CLAIMED,
    RECEIPT_REJECTED,
    RECEIPT_RELEASED,
    ReceiptRecord,
)

# 目录行状态
LINE_REJECTED = "rejected"                  # 渠道已拒绝该组报价
LINE_ACTIVE = "active"                      # 可接单且可承诺量满足
LINE_SHORT = "short"                        # 可承诺量不足
LINE_BLOCKED = "blocked"                    # 资格类阻断（声明不确定/无适用声明）
LINE_LABEL_GAP = "label_gap"                # 有实物但缺已证实标签
LINE_CLAIMED = "claimed"                    # 认领在效且履约量齐备
LINE_CLAIMED_AT_RISK = "claimed_at_risk"    # 已认领但受更正影响，需人工处置
LINE_RELEASED = "released"                  # 旧认领已释放，库存待重新确认

# 可售量分桶原因码
REASON_DIRECT = "direct_supply"
REASON_IDENTICAL_CONVERSION = "identical_conversion"
REASON_HOLD = "held_label_pending"
REASON_LABEL_MISSING = "label_missing"
REASON_UNPACK_ONLY = "unpack_only_not_sellable"
REASON_UNRESOLVED = "unresolved_conversion"
REASON_PARENT_DISQUALIFIED = "parent_subcomponent_unresolved"
REASON_LOCKED = "locked_by_receipt"


@dataclass(frozen=True)
class SupplyBucket:
    """一条供给来源及其可售/受阻分量。"""

    lot: str
    package: str
    label: str | None
    on_hand: int
    sellable: int
    held: int
    unlabeled: int
    reason: str
    conversion: ResolvedEdge | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class LockedAllocation:
    """一条生效中的回执锁定。"""

    receipt: ReceiptRecord
    lot: str
    units: int


@dataclass(frozen=True)
class LineEvaluation:
    """一条目录行在当前时点的完整判定结果。"""

    line: ChannelLine
    latest_receipt: ReceiptRecord | None
    state: str
    locked_units: int
    locks: tuple[LockedAllocation, ...]
    committable_units: int
    committed_plan: tuple[tuple[str, int], ...]
    supply: tuple[SupplyBucket, ...]
    blockers: tuple[str, ...]
    evidence: tuple[str, ...]

    @property
    def demand_remaining(self) -> int:
        return max(self.line.hold_units - self.locked_units, 0)


class AvailabilityEngine:
    """汇总目录、声明、台账与回执，给出逐行判定与渠道占用方案。"""

    def __init__(self, batches: list[IngestedBatch], claims: ClaimBook,
                 inventory: Inventory):
        self.batches = sorted(batches, key=lambda b: b.arrival)
        self.claims = claims
        self.inventory = inventory
        self.lines: dict[str, ChannelLine] = {}
        self.line_history: dict[str, list[ChannelLine]] = {}
        self.receipts_of: dict[str, list[ReceiptRecord]] = {}
        for batch in self.batches:
            for line in batch.channels:
                self.line_history.setdefault(line.line, []).append(line)
                self.lines[line.line] = line
            for receipt in batch.receipts:
                self.receipts_of.setdefault(receipt.line, []).append(receipt)

    # ---- 回执与锁定 -------------------------------------------------

    def latest_receipt(self, line_id: str) -> ReceiptRecord | None:
        history = self.receipts_of.get(line_id, ())
        if not history:
            return None
        return max(history, key=lambda receipt: receipt.source_arrival)

    def active_locks(self, line_id: str) -> list[LockedAllocation]:
        receipt = self.latest_receipt(line_id)
        if receipt is None or receipt.state != RECEIPT_CLAIMED:
            return []
        return [LockedAllocation(receipt=receipt, lot=lot, units=qty)
                for lot, qty in receipt.allocation]

    def locked_lots(self) -> dict[str, int]:
        """全部生效认领按批次汇总的占用量。"""
        totals: dict[str, int] = {}
        for line_id in self.lines:
            for lock in self.active_locks(line_id):
                totals[lock.lot] = totals.get(lock.lot, 0) + lock.units
        return totals

    # ---- 供给分桶 ---------------------------------------------------

    def supply_for(self, line: ChannelLine,
                   locked_map: dict[str, int]) -> list[SupplyBucket]:
        """列出该行所需包装单位的全部实物来源及可售分量。"""
        buckets: list[SupplyBucket] = []
        direct_ledgers = self.inventory.lots_in_package(line.unit)
        for ledger in direct_ledgers:
            buckets.extend(self._direct_buckets(line, ledger, locked_map))
        # 父包装折算只在没有任何同单位实物批次时才作为候选供给：
        # 已实拆的盒属于独立批次池，未拆外箱不得再折算成盒，
        # 避免一个外箱同时承诺整箱与拆盒两个渠道。
        if direct_ledgers:
            return buckets
        # 等价关系折算：父包装池 -> 子包装行
        for parent in self.inventory.all_lots():
            if parent.is_derived or parent.package == line.unit:
                continue
            for edge in self.claims.child_edges(parent.lot, parent.package):
                if edge.child != line.unit:
                    continue
                converted = parent.on_hand_units * edge.factor
                if edge.uncertain:
                    buckets.append(SupplyBucket(
                        lot=parent.lot, package=parent.package, label=None,
                        on_hand=parent.on_hand_units, sellable=0, held=0,
                        unlabeled=converted, reason=REASON_UNRESOLVED,
                        conversion=edge, evidence=parent.evidence))
                elif not edge.sellable_conversion:
                    buckets.append(SupplyBucket(
                        lot=parent.lot, package=parent.package, label=None,
                        on_hand=parent.on_hand_units, sellable=0, held=0,
                        unlabeled=converted, reason=REASON_UNPACK_ONLY,
                        conversion=edge, evidence=parent.evidence))
                else:
                    # identical：折算后的可售量仍受父批次标签与整箱资格约束
                    confirmed = parent.confirmed_for(line.requires_label)
                    sellable = min(confirmed, parent.on_hand_units) * edge.factor
                    buckets.append(SupplyBucket(
                        lot=parent.lot, package=parent.package,
                        label=line.requires_label, on_hand=parent.on_hand_units,
                        sellable=sellable, held=0,
                        unlabeled=max(converted - sellable, 0),
                        reason=REASON_IDENTICAL_CONVERSION, conversion=edge,
                        evidence=parent.evidence))
        return buckets

    def _direct_buckets(self, line: ChannelLine, ledger: LotLedger,
                        locked_map: dict[str, int]) -> list[SupplyBucket]:
        buckets: list[SupplyBucket] = []
        locked_here = locked_map.get(ledger.lot, 0)
        free = max(ledger.on_hand_units - locked_here, 0)
        if locked_here:
            # 锁定量单独成池：是已认领的真实承诺，不参与新一轮可承诺分配
            buckets.append(SupplyBucket(
                lot=ledger.lot, package=ledger.package,
                label=line.requires_label, on_hand=locked_here,
                sellable=locked_here, held=0, unlabeled=0,
                reason=REASON_LOCKED, evidence=ledger.evidence))

        # 自由池按已证实标签分量拆分，hold/无标签量不得默认可售。
        # 锁定量视为先占用 confirmed 分量（回执锁定本身要求标签已证实），
        # 因此各标签分量先按锁定量对应削减，再在自由池内分桶。
        confirmed_total = ledger.confirmed_for(line.requires_label)
        hold_total = ledger.hold_for(line.requires_label)
        missing_total = ledger.label_missing_units
        parent_unresolved = (BLOCK_SUBCOMPONENT_UNRESOLVED in ledger.block_reasons
                             and not ledger.is_derived)
        confirmed_free = max(min(confirmed_total - locked_here, free), 0)
        hold_free = min(hold_total, max(free - confirmed_free, 0))
        unlabeled = min(missing_total, max(free - confirmed_free - hold_free, 0))
        if parent_unresolved:
            # 任一子组件关系不确定：整箱报价整体受阻，已证实标签也不能自动放行
            buckets.append(SupplyBucket(
                lot=ledger.lot, package=ledger.package,
                label=line.requires_label, on_hand=free, sellable=0, held=0,
                unlabeled=free, reason=REASON_PARENT_DISQUALIFIED,
                evidence=ledger.evidence))
            return buckets
        if confirmed_free:
            buckets.append(SupplyBucket(
                lot=ledger.lot, package=ledger.package,
                label=line.requires_label, on_hand=free, sellable=confirmed_free,
                held=0, unlabeled=0, reason=REASON_DIRECT, evidence=ledger.evidence))
        if hold_free:
            buckets.append(SupplyBucket(
                lot=ledger.lot, package=ledger.package,
                label=line.requires_label, on_hand=ledger.on_hand_units,
                sellable=0, held=hold_free, unlabeled=0,
                reason=REASON_HOLD, evidence=ledger.evidence))
        if unlabeled:
            reason = (REASON_PARENT_DISQUALIFIED
                      if BLOCK_SUBCOMPONENT_UNRESOLVED in ledger.block_reasons
                      and not ledger.is_derived
                      else REASON_LABEL_MISSING)
            buckets.append(SupplyBucket(
                lot=ledger.lot, package=ledger.package, label=None,
                on_hand=ledger.on_hand_units, sellable=0, held=0,
                unlabeled=unlabeled, reason=reason, evidence=ledger.evidence))
        return buckets

    # ---- 逐行判定 ---------------------------------------------------

    def evaluate(self) -> dict[str, LineEvaluation]:
        locked_map = self.locked_lots()
        results: dict[str, LineEvaluation] = {}
        # 渠道顺序：rank 小者优先，同级按渠道标识（登记顺序由 channel 名称稳定排序）
        ordered = sorted(self.lines.values(), key=lambda line: (line.rank, line.channel, line.line))
        # 已被前序渠道在本轮承诺中占用的自由量
        planned: dict[str, int] = {}
        for line in ordered:
            results[line.line] = self._evaluate_line(line, locked_map, planned)
        return results

    def _evaluate_line(self, line: ChannelLine, locked_map: dict[str, int],
                       planned: dict[str, int]) -> LineEvaluation:
        receipt = self.latest_receipt(line.line)
        locks = tuple(self.active_locks(line.line))
        locked_units = sum(lock.units for lock in locks)
        supply = self.supply_for(line, locked_map)

        blockers: list[str] = []
        evidence: list[str] = []
        for bucket in supply:
            evidence.extend(bucket.evidence)
        if receipt is not None:
            evidence.append(f"receipt:arrival{receipt.source_arrival}:{receipt.state}")

        # 已拒绝或已释放的行不得再自动生成承诺：释放的库存要等新渠道回执确认
        terminal = receipt is not None and receipt.state in (RECEIPT_REJECTED,
                                                              RECEIPT_RELEASED)
        free_sellable = [bucket for bucket in supply
                         if bucket.reason in (REASON_DIRECT, REASON_IDENTICAL_CONVERSION)
                         and bucket.sellable]
        plan: list[tuple[str, int]] = []
        need = line.hold_units - locked_units
        if not terminal:
            for bucket in sorted(free_sellable, key=lambda b: (b.lot, b.package)):
                already = planned.get(bucket.lot, 0)
                available = max(bucket.sellable - already, 0)
                if available <= 0 or need <= 0:
                    continue
                take = min(available, need)
                plan.append((bucket.lot, take))
                planned[bucket.lot] = already + take
                need -= take
        committable = sum(qty for _, qty in plan)

        # 阻断原因分两类：
        # - 行级硬阻断：所有供给来源都不具资格（全部 unresolved/无适用声明/仅 unpack）；
        # - 局部警告：部分批次受阻但另有合规来源，记录在 blockers 供追溯，不改变行状态。
        blocking_reasons = {
            REASON_PARENT_DISQUALIFIED, REASON_UNRESOLVED, REASON_UNPACK_ONLY}
        if supply and all(bucket.reason in blocking_reasons for bucket in supply):
            if any(bucket.reason in (REASON_PARENT_DISQUALIFIED, REASON_UNRESOLVED)
                   for bucket in supply):
                blockers.append(BLOCK_SUBCOMPONENT_UNRESOLVED)
            if all(bucket.reason == REASON_UNPACK_ONLY for bucket in supply):
                blockers.append(BLOCK_NO_APPLICABLE_CLAIM)
        elif any(bucket.reason in (REASON_PARENT_DISQUALIFIED, REASON_UNRESOLVED)
                 for bucket in supply):
            blockers.append(BLOCK_SUBCOMPONENT_UNRESOLVED)
        if any(bucket.reason == REASON_HOLD for bucket in supply):
            blockers.append(BLOCK_LABEL_PENDING)
        if (any(bucket.reason == REASON_LABEL_MISSING for bucket in supply)
                and not free_sellable):
            blockers.append(BLOCK_LABEL_MISSING)
        if not supply:
            blockers.append(BLOCK_NO_APPLICABLE_CLAIM)

        hard_blocked = (BLOCK_SUBCOMPONENT_UNRESOLVED in blockers
                        and not free_sellable) or (
            BLOCK_NO_APPLICABLE_CLAIM in blockers and not free_sellable)
        state = self._state(line, receipt, locked_units, committable, plan,
                            blockers, hard_blocked)
        return LineEvaluation(
            line=line, latest_receipt=receipt, state=state,
            locked_units=locked_units, locks=locks,
            committable_units=committable, committed_plan=tuple(plan),
            supply=tuple(supply), blockers=tuple(dict.fromkeys(blockers)),
            evidence=tuple(dict.fromkeys(evidence)),
        )

    def _state(self, line: ChannelLine, receipt: ReceiptRecord | None,
               locked_units: int, committable: int,
               plan: list[tuple[str, int]], blockers: list[str],
               hard_blocked: bool) -> str:
        if receipt is not None and receipt.state == RECEIPT_REJECTED:
            return LINE_REJECTED
        if receipt is not None and receipt.state == RECEIPT_RELEASED:
            return LINE_RELEASED
        if receipt is not None and receipt.state == RECEIPT_CLAIMED:
            # 认领在效：受阻码只影响风险提示，不改变已锁定事实
            if BLOCK_LABEL_PENDING in blockers or (
                    BLOCK_SUBCOMPONENT_UNRESOLVED in blockers
                    and not hard_blocked):
                return LINE_CLAIMED_AT_RISK
            return LINE_CLAIMED
        # 尚无生效认领的行按可承诺量判定
        if hard_blocked:
            return LINE_BLOCKED
        if not committable and BLOCK_LABEL_MISSING in blockers:
            return LINE_LABEL_GAP
        if locked_units + committable >= line.hold_units:
            return LINE_ACTIVE
        return LINE_SHORT
