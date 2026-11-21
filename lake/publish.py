"""版本化发布：按交接批次重放，输出渠道实际读取的行数据与库存承诺。

每个 publication 版本对应一次交接时点：
- catalog_lines.jsonl   目录行版本化数据（状态、可售/锁定/可承诺量、阻断原因）
- lot_ledger.jsonl      批次台账（实物量、标签分量、适用声明、证据）
- commitments.jsonl     库存承诺（生效锁定 + 本轮可承诺占用建议，互斥分池）
- manifest.json         版本清单与全局汇总

原始层不可变、发布层可重建：删除 published/ 后用相同输入可得到相同结果。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .availability import (
    AvailabilityEngine,
    REASON_DIRECT,
    REASON_IDENTICAL_CONVERSION,
    REASON_LOCKED,
)
from .catalog import ClaimBook
from .ingest import load_all, load_registry
from .inventory import Inventory
from .models import IngestedBatch


@dataclass(frozen=True)
class Publication:
    version: int
    as_of: str
    path: Path
    line_count: int
    lot_count: int
    commitment_count: int


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            stream.write("\n")


def default_as_of(batches: list[IngestedBatch]) -> date:
    """重放时点：本批及之前全部实物记录中的最晚业务日。"""
    days = [rec.at for batch in batches for rec in batch.stock]
    return max(days) if days else date.today()


def build_snapshot(batches: list[IngestedBatch], as_of: date) -> dict:
    """根据截至某时点的全部批次计算发布快照。"""
    version = max((batch.arrival for batch in batches), default=0)
    claims = ClaimBook(batches)
    inventory = Inventory(batches, claims)
    engine = AvailabilityEngine(batches, claims, inventory)
    evaluations = engine.evaluate()

    line_rows: list[dict] = []
    commitment_rows: list[dict] = []
    for line_id in sorted(evaluations):
        evaluation = evaluations[line_id]
        line = evaluation.line
        free_sellable = sum(bucket.sellable for bucket in evaluation.supply
                            if bucket.reason in (REASON_DIRECT, REASON_IDENTICAL_CONVERSION))
        blocked_sellable = sum(bucket.held for bucket in evaluation.supply)
        unlabeled = sum(bucket.unlabeled for bucket in evaluation.supply
                        if bucket.reason not in (REASON_LOCKED,))
        row = {
            "publication_version": version,
            "as_of": as_of.isoformat(),
            "channel": line.channel,
            "line": line.line,
            "sku": line.sku,
            "unit": line.unit,
            "requires_label": line.requires_label,
            "rank": line.rank,
            "hold_units": line.hold_units,
            "state": evaluation.state,
            "locked_units": evaluation.locked_units,
            "committable_units": evaluation.committable_units,
            "free_sellable_units": free_sellable,
            "held_pending_units": blocked_sellable,
            "unverified_units": unlabeled,
            "blockers": list(evaluation.blockers),
            "latest_receipt": (evaluation.latest_receipt.state
                               if evaluation.latest_receipt else None),
            "committed_plan": [{"lot": lot, "units": qty}
                               for lot, qty in evaluation.committed_plan],
            "evidence": list(evaluation.evidence),
        }
        line_rows.append(row)
        for lock in evaluation.locks:
            commitment_rows.append({
                "publication_version": version,
                "line": line.line, "channel": line.channel, "unit": line.unit,
                "kind": "locked", "lot": lock.lot, "units": lock.units,
                "receipt_publication": lock.receipt.publication,
                "source_arrival": lock.receipt.source_arrival,
            })
        for lot, qty in evaluation.committed_plan:
            commitment_rows.append({
                "publication_version": version,
                "line": line.line, "channel": line.channel, "unit": line.unit,
                "kind": "committable", "lot": lot, "units": qty,
                "receipt_publication": None, "source_arrival": None,
            })

    lot_rows: list[dict] = []
    for ledger in inventory.all_lots():
        lot_rows.append({
            "publication_version": version,
            "as_of": as_of.isoformat(),
            "lot": ledger.lot,
            "package": ledger.package,
            "origin": ledger.origin,
            "is_derived": ledger.is_derived,
            "produced_at": ledger.produced_at.isoformat(),
            "received_units": ledger.received_units,
            "opened_units": ledger.opened_units,
            "on_hand_units": ledger.on_hand_units,
            "confirmed_total": ledger.confirmed_total(),
            "hold_total": ledger.hold_total(),
            "label_missing_units": ledger.label_missing_units,
            "label_breakdown": [asdict(item) for item in ledger.label_breakdown],
            "applicable_claims": sorted({edge.claim for edge in ledger.applicable_edges}),
            "conversion_scopes": sorted({edge.scope for edge in ledger.applicable_edges}),
            "unpack_time_scope": (ledger.unpack_edge.scope
                                  if ledger.unpack_edge is not None else None),
            "unpack_time_claim": (ledger.unpack_edge.claim
                                  if ledger.unpack_edge is not None else None),
            "block_reasons": list(ledger.block_reasons),
            "evidence": list(ledger.evidence),
        })

    return {"lines": line_rows, "lots": lot_rows, "commitments": commitment_rows,
            "evaluations": evaluations}


class Publisher:
    """把快照落为版本化文件并维护 current 指针。"""

    def __init__(self, lake_home: Path):
        self.lake_home = Path(lake_home)
        self.published = self.lake_home / "published"

    def publish_all(self) -> list[Publication]:
        """对原始层中每个交接时点逐一重放并发布。"""
        registry = load_registry(self.lake_home)
        if not registry["arrivals"]:
            return []
        all_batches = load_all(self.lake_home)
        publications: list[Publication] = []
        cumulative: list[IngestedBatch] = []
        for batch in all_batches:
            cumulative.append(batch)
            publications.append(self._publish_version(batch.arrival, cumulative))
        return publications

    def _publish_version(self, version: int,
                         batches: list[IngestedBatch]) -> Publication:
        as_of = default_as_of(batches)
        snapshot = build_snapshot(batches, as_of)
        out_dir = self.published / f"v{version:03d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_jsonl(out_dir / "catalog_lines.jsonl", snapshot["lines"])
        _write_jsonl(out_dir / "lot_ledger.jsonl", snapshot["lots"])
        _write_jsonl(out_dir / "commitments.jsonl", snapshot["commitments"])
        manifest = {
            "version": version,
            "as_of": as_of.isoformat(),
            "arrivals_included": [batch.arrival for batch in batches],
            "line_count": len(snapshot["lines"]),
            "lot_count": len(snapshot["lots"]),
            "commitment_count": len(snapshot["commitments"]),
            "line_states": {
                state: sum(1 for row in snapshot["lines"] if row["state"] == state)
                for state in sorted({row["state"] for row in snapshot["lines"]})
            },
        }
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        current = self.published / "current"
        current.mkdir(exist_ok=True)
        for name in ("catalog_lines.jsonl", "lot_ledger.jsonl",
                     "commitments.jsonl", "manifest.json"):
            target = current / name
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(Path("..") / f"v{version:03d}" / name)
        return Publication(
            version=version, as_of=as_of.isoformat(), path=out_dir,
            line_count=manifest["line_count"], lot_count=manifest["lot_count"],
            commitment_count=manifest["commitment_count"])


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]
