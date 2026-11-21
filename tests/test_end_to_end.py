"""用 examples/ 合成交接资料做端到端核对。

每条断言对应交接资料说明中的一个业务事实，防止后续维护悄悄改变判定口径。
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from lake.availability import (
    LINE_ACTIVE,
    LINE_CLAIMED_AT_RISK,
    LINE_REJECTED,
    LINE_RELEASED,
    REASON_HOLD,
    REASON_LOCKED,
    REASON_PARENT_DISQUALIFIED,
)
from lake.ingest import ingest_dir, load_all, load_registry
from lake.catalog import ClaimBook
from lake.inventory import (
    BLOCK_LABEL_MISSING,
    BLOCK_LABEL_PENDING,
    BLOCK_NO_APPLICABLE_CLAIM,
    BLOCK_SUBCOMPONENT_UNRESOLVED,
    Inventory,
)
from lake.availability import AvailabilityEngine
from lake.publish import Publisher, read_jsonl
from lake.trace import (
    DISAPPLY_OVERRIDE,
    DISPOSITION_PAUSE,
    DISPOSITION_RELABEL,
    OverrideBook,
    Tracer,
    apply_overrides_to_claims,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "lake"
        ingest_dir(self.home, EXAMPLES)
        self.batches = load_all(self.home)
        self.claims = ClaimBook(self.batches)
        self.inventory = Inventory(self.batches, self.claims)
        self.engine = AvailabilityEngine(self.batches, self.claims, self.inventory)
        self.evaluations = self.engine.evaluate()

    def tearDown(self):
        self._tmp.cleanup()

    def test_all_seven_arrivals_landed_immutably(self):
        registry = sorted(int(key) for key in
                          load_registry(self.home)["arrivals"])
        self.assertEqual(registry, [1, 2, 3, 4, 5, 6, 7])
        # 原始层保留每个批次的原件
        for arrival in registry:
            raw_dir = self.home / "raw" / f"arrival_{arrival:03d}"
            self.assertTrue((raw_dir / "manifest.json").exists())

    def test_case_with_two_child_packaging_types_kept_distinct(self):
        # S18 的 CASE-A 同时含 BOX-10(4) 与 BOX-06(2)，修订只动 BOX-10
        box10 = self.claims.edge("S18", "CASE-A", "BOX-10", "L01")
        box06 = self.claims.edge("S18", "CASE-A", "BOX-06", "L01")
        self.assertEqual(box10.scope, "unresolved")
        self.assertEqual(box06.scope, "unpack")
        self.assertEqual(len(box10.history), 2)

    def test_parent_and_derived_quantities_without_double_count(self):
        self.assertEqual(self.inventory.get("L01").on_hand_units, 5)   # 7-2
        self.assertEqual(self.inventory.get("L01-A").on_hand_units, 8)
        self.assertEqual(self.inventory.get("L01-B").on_hand_units, 4)
        self.assertEqual(self.inventory.get("L02").on_hand_units, 4)   # 5-1
        self.assertEqual(self.inventory.get("L02-A").on_hand_units, 6)

    def test_relabel_certificates_split_confirmed_and_hold(self):
        ledger = self.inventory.get("L01-A")
        self.assertEqual(ledger.confirmed_for("XB-10"), 6)
        self.assertEqual(ledger.hold_for("XB-10"), 2)
        self.assertEqual(ledger.on_hand_units, 8)  # 实物量不变
        self.assertIn(BLOCK_LABEL_PENDING, ledger.block_reasons)

    def test_unlabeled_lot_only_reports_verified_quantity(self):
        ledger = self.inventory.get("L03")
        self.assertEqual(ledger.on_hand_units, 3)
        self.assertEqual(ledger.confirmed_total(), 0)
        self.assertEqual(ledger.label_missing_units, 3)
        self.assertIn(BLOCK_LABEL_MISSING, ledger.block_reasons)
        self.assertIn(BLOCK_NO_APPLICABLE_CLAIM, ledger.block_reasons)

    def test_l01_case_quote_blocked_by_unresolved_subcomponent(self):
        self.assertIn(BLOCK_SUBCOMPONENT_UNRESOLVED,
                      self.inventory.get("L01").block_reasons)
        v20 = self.evaluations["V20"]
        reasons = {bucket.reason for bucket in v20.supply}
        self.assertIn(REASON_PARENT_DISQUALIFIED, reasons)

    def test_line_states_match_hand_judgment(self):
        self.assertEqual(self.evaluations["V20"].state, LINE_RELEASED)
        self.assertEqual(self.evaluations["V21"].state, LINE_CLAIMED_AT_RISK)
        self.assertEqual(self.evaluations["V30"].state, LINE_ACTIVE)
        self.assertEqual(self.evaluations["V31"].state, LINE_REJECTED)
        self.assertEqual(self.evaluations["V32"].state, LINE_ACTIVE)

    def test_unlabeled_l3_never_enters_any_commitment(self):
        for line_id, evaluation in self.evaluations.items():
            planned_lots = {lot for lot, _ in evaluation.committed_plan}
            locked_lots = {lock.lot for lock in evaluation.locks}
            self.assertNotIn("L03", planned_lots,
                             f"{line_id} 把无标签 L03 写成了可承诺库存")
            self.assertNotIn("L03", locked_lots)
        # 发布的承诺文件中同样不得出现 L03
        Publisher(self.home).publish_all()
        commitments = read_jsonl(self.home / "published" / "current"
                                 / "commitments.jsonl")
        self.assertFalse(any(row["lot"] == "L03" for row in commitments))

    def test_locked_receipt_and_commitments_use_separate_pools(self):
        v21 = self.evaluations["V21"]
        self.assertEqual(v21.locked_units, 6)
        self.assertEqual({lock.lot for lock in v21.locks}, {"L01-A"})
        # 锁定量以 locked 桶出现，自由池里只剩 hold 的 2 个
        reasons = {bucket.reason for bucket in v21.supply}
        self.assertIn(REASON_LOCKED, reasons)
        self.assertIn(REASON_HOLD, reasons)
        # V30 整箱承诺只能落在 L02，不能动用已被 V20 历史锁定过的 L01 余箱之外的盒池
        v30 = self.evaluations["V30"]
        self.assertEqual(v30.committed_plan, (("L02", 4),))

    def test_released_line_has_no_new_commitment(self):
        self.assertEqual(self.evaluations["V20"].committed_plan, ())
        self.assertEqual(self.evaluations["V20"].committable_units, 0)

    def test_channel_rank_ordering_is_wholesale_retail_local(self):
        # rank 1 wholesale -> 2 retail -> 3 local
        ordered = sorted(self.engine.lines.values(),
                         key=lambda line: (line.rank, line.channel, line.line))
        self.assertEqual([line.line for line in ordered][:2], ["V20", "V30"])

    def test_publication_versions_and_current_pointer(self):
        pubs = Publisher(self.home).publish_all()
        self.assertEqual([pub.version for pub in pubs], [1, 2, 3, 4, 5, 6, 7])
        current = self.home / "published" / "current"
        self.assertTrue((current / "catalog_lines.jsonl").is_symlink())
        lines = read_jsonl(current / "catalog_lines.jsonl")
        self.assertEqual({row["line"] for row in lines},
                         {"V20", "V21", "V30", "V31", "V32"})
        # 发布行带证据链
        self.assertTrue(all(row["evidence"] for row in lines))

    def test_trace_claimed_line_relabel_disposition(self):
        result = Tracer(self.batches, self.claims, self.inventory,
                        self.engine).trace("V21")
        self.assertEqual(result.channel_decision, "claimed")
        self.assertEqual(result.disposition, DISPOSITION_RELABEL)
        locked = [item for item in result.fulfillment if item.status == "locked"]
        self.assertEqual(sum(item.fulfillable_units for item in locked), 6)

    def test_trace_released_line_pause_and_original_lot_lookup(self):
        result = Tracer(self.batches, self.claims, self.inventory,
                        self.engine).trace("V20")
        self.assertEqual(result.disposition, DISPOSITION_PAUSE)
        # 仍能沿目录行查回原批次与原单位
        lots = {item.lot for item in result.fulfillment}
        self.assertIn("L01", lots)
        self.assertTrue(any(item["state"] == "released"
                            for item in result.receipt_timeline))
        self.assertTrue(any(item["state"] == "claimed"
                            for item in result.receipt_timeline))

    def test_manual_disapply_rejudges_without_physical_change(self):
        OverrideBook(self.home).disapply("S19", "L02", "厂家通知不适用", 7)
        view = apply_overrides_to_claims(self.claims, {("S19", "L02")})
        inventory = Inventory(self.batches, view)
        engine = AvailabilityEngine(self.batches, view, inventory)
        # 实物量不变
        self.assertEqual(inventory.get("L02").on_hand_units, 4)
        self.assertEqual(inventory.get("L02-A").on_hand_units, 6)
        # 可出货资格变化：L02 整箱受阻
        self.assertIn(BLOCK_SUBCOMPONENT_UNRESOLVED,
                      inventory.get("L02").block_reasons)
        result = Tracer(self.batches, view, inventory, engine).trace("V30")
        self.assertTrue(any(impact.kind == DISAPPLY_OVERRIDE
                            for impact in result.impacts))
        # 派生批次 L02-A 被列入随批重判
        impacted_lots = {impact.detail for impact in result.impacts}
        self.assertTrue(any("L02-A" in detail for detail in impacted_lots))


if __name__ == "__main__":
    unittest.main()
