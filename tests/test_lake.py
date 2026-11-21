"""数据湖单元测试。

测试用内联构造的最小交接资料覆盖业务规则，不依赖 examples/ 的具体数值；
examples/ 的端到端核对放在 test_end_to_end.py。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from lake.availability import (
    AvailabilityEngine,
    LINE_ACTIVE,
    LINE_BLOCKED,
    LINE_CLAIMED,
    LINE_CLAIMED_AT_RISK,
    LINE_LABEL_GAP,
    LINE_REJECTED,
    LINE_RELEASED,
    LINE_SHORT,
    REASON_HOLD,
    REASON_LOCKED,
    REASON_UNPACK_ONLY,
)
from lake.catalog import ClaimBook, EXPIRED
from lake.ingest import IngestError, cross_check, ingest_dir, load_all
from lake.inventory import (
    BLOCK_LABEL_MISSING,
    BLOCK_LABEL_PENDING,
    BLOCK_SUBCOMPONENT_UNRESOLVED,
    Inventory,
)
from lake.models import IngestedBatch, parse_day
from lake.publish import Publisher, read_jsonl
from lake.trace import (
    DISAPPLY_OVERRIDE,
    DISPOSITION_EXCHANGE,
    DISPOSITION_PAUSE,
    DISPOSITION_RELABEL,
    OverrideBook,
    Tracer,
    apply_overrides_to_claims,
)

HEADERS = {
    "units.csv": "arrival,claim,package,child,factor,lot,from,until,scope",
    "stock.csv": "arrival,lot,origin,at,package,label,units",
    "stock_moves.csv": "arrival,move,origin,used,children",
    "channel.csv": "arrival,channel,line,sku,unit,requires_label,rank,hold_units",
    "receipt.csv": "arrival,channel,line,publication,state,allocation",
    "relabel.csv": "arrival,certificate,lot,action,label,scope_units",
}


class Fixture:
    """在临时目录中按交接批次追加 CSV 行。"""

    def __init__(self, root: Path):
        self.root = root
        for name, header in HEADERS.items():
            (root / name).write_text(header + "\n", encoding="utf-8")

    def add(self, name: str, rows: list[str]) -> None:
        with (self.root / name).open("a", encoding="utf-8") as stream:
            for row in rows:
                stream.write(row + "\n")


class LakeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "lake"
        self.source = Path(self._tmp.name) / "exchange"
        self.source.mkdir()
        self.fixture = Fixture(self.source)

    def tearDown(self):
        self._tmp.cleanup()

    def build(self):
        ingest_dir(self.home, self.source)
        batches = load_all(self.home)
        claims = ClaimBook(batches)
        inventory = Inventory(batches, claims)
        engine = AvailabilityEngine(batches, claims, inventory)
        return batches, claims, inventory, engine

    # ---- 导入与校验 -------------------------------------------------

    def test_invalid_scope_is_rejected_as_a_batch(self):
        self.fixture.add("units.csv", [
            "1,S1,CASE,BOX,4,L1,2026-06-01,,bogus",
        ])
        with self.assertRaises(IngestError) as caught:
            ingest_dir(self.home, self.source)
        self.assertTrue(any("scope" in issue for issue in caught.exception.issues))
        self.assertFalse((self.home / "raw").exists())

    def test_move_quantity_must_reconcile_with_claim_factor(self):
        self.fixture.add("units.csv", [
            "1,S1,CASE,BOX,4,L1,2026-06-01,,unpack",
        ])
        self.fixture.add("stock.csv", [
            "2,L1,,2026-09-01,CASE,XC,10",
            "3,L1-A,L1,2026-09-02,BOX,XB,99",  # 4 因子拆 1 箱应是 4 而不是 99
        ])
        self.fixture.add("stock_moves.csv", ["3,OP1,L1,1,L1-A"])
        with self.assertRaises(IngestError) as caught:
            ingest_dir(self.home, self.source)
        self.assertTrue(any("勾稽" in issue for issue in caught.exception.issues))

    def test_claimed_receipt_requires_allocation(self):
        self.fixture.add("channel.csv", [
            "2,ch,V1,P,CASE,XC,1,4",
        ])
        self.fixture.add("receipt.csv", ["3,ch,V1,P1,claimed,"])
        with self.assertRaises(IngestError) as caught:
            ingest_dir(self.home, self.source)
        self.assertTrue(any("allocation" in issue for issue in caught.exception.issues))

    def test_reimport_identical_files_is_idempotent(self):
        self.fixture.add("stock.csv", ["2,L1,,2026-09-01,CASE,XC,10"])
        first = ingest_dir(self.home, self.source)
        second = ingest_dir(self.home, self.source)
        self.assertEqual([batch.arrival for batch in first], [2])
        self.assertEqual(second, [])

    # ---- 声明修订与有效期 -------------------------------------------

    def test_claim_revision_only_touches_named_child(self):
        self.fixture.add("units.csv", [
            "1,S1,CASE,BOX-A,4,L1,2026-06-01,,unpack",
            "1,S1,CASE,BOX-B,2,L1,2026-06-01,,unpack",
            "5,S1,CASE,BOX-A,4,L1,2026-06-01,,unresolved",
        ])
        batches, claims, _inv, _engine = self.build()
        box_a = claims.edge("S1", "CASE", "BOX-A", "L1")
        box_b = claims.edge("S1", "CASE", "BOX-B", "L1")
        self.assertEqual(box_a.scope, "unresolved")
        self.assertEqual(box_b.scope, "unpack")  # 未提及的子类继续有效
        self.assertEqual(len(box_a.history), 2)

    def test_derived_lot_keeps_unpack_time_scope_separate_from_current(self):
        self.fixture.add("units.csv", [
            "1,S1,CASE,BOX,4,L1,2026-06-01,,unpack",
            "5,S1,CASE,BOX,4,L1,2026-06-01,,unresolved",
        ])
        self.fixture.add("stock.csv", [
            "2,L1,,2026-09-01,CASE,XC,4",
            "3,L1-A,L1,2026-09-02,BOX,XB,8",
        ])
        self.fixture.add("stock_moves.csv", ["3,OP1,L1,2,L1-A"])
        _b, _c, inventory, _e = self.build()
        ledger = inventory.get("L1-A")
        # 翻包发生在修订之前：来源资格仍是 unpack，当前资格已变 unresolved
        self.assertEqual(ledger.unpack_edge.scope, "unpack")
        self.assertEqual(ledger.source_edge.scope, "unresolved")
        # 已实拆货不被追溯否定，台账无来源资格阻断
        self.assertNotIn("no_applicable_claim", ledger.block_reasons)

    def test_claim_window_is_left_closed_right_open(self):
        self.fixture.add("units.csv", [
            "1,S1,CASE,BOX,4,L1,2026-06-01,2026-10-01,unpack",
        ])
        batches, claims, _inv, _engine = self.build()
        self.assertEqual(claims.edge("S1", "CASE", "BOX", "L1",
                                     on_day=date(2026, 9, 30)).window_state, "active")
        self.assertEqual(claims.edge("S1", "CASE", "BOX", "L1",
                                     on_day=date(2026, 10, 1)).window_state, EXPIRED)

    def test_expired_claim_is_not_applicable_to_later_receipt(self):
        self.fixture.add("units.csv", [
            "1,S1,CASE,BOX,4,L1,2026-06-01,2026-10-01,unpack",
        ])
        # 实收日晚于声明结束日：该声明对本批货不适用
        self.fixture.add("stock.csv", ["2,L1,,2026-10-02,CASE,XC,5"])
        _b, _c, inventory, _e = self.build()
        ledger = inventory.get("L1")
        self.assertEqual(ledger.applicable_edges, ())
        self.assertIn("no_applicable_claim", ledger.block_reasons)

    # ---- 台账：实物量与标签 -----------------------------------------

    def test_parent_and_derived_lots_are_booked_separately(self):
        self.fixture.add("units.csv", [
            "1,S1,CASE,BOX,4,L1,2026-06-01,,unpack",
        ])
        self.fixture.add("stock.csv", [
            "2,L1,,2026-09-01,CASE,XC,7",
            "3,L1-A,L1,2026-09-02,BOX,XB,8",
        ])
        self.fixture.add("stock_moves.csv", ["3,OP1,L1,2,L1-A"])
        _b, _c, inventory, _e = self.build()
        parent = inventory.get("L1")
        derived = inventory.get("L1-A")
        self.assertEqual(parent.received_units, 7)
        self.assertEqual(parent.opened_units, 2)
        self.assertEqual(parent.on_hand_units, 5)       # 余 5 整箱
        self.assertEqual(derived.on_hand_units, 8)      # 盒独立记账
        self.assertEqual(sum(ledger.on_hand_units for ledger in inventory.all_lots()), 13)

    def test_missing_label_is_never_assumed_from_catalog(self):
        self.fixture.add("stock.csv", ["4,L3,,2026-10-05,CASE,,3"])
        _b, _c, inventory, _e = self.build()
        ledger = inventory.get("L3")
        self.assertEqual(ledger.label_missing_units, 3)
        self.assertIn(BLOCK_LABEL_MISSING, ledger.block_reasons)
        self.assertEqual(ledger.confirmed_total(), 0)

    def test_relabel_latest_certificate_wins_not_summed(self):
        self.fixture.add("units.csv", [
            "1,S1,CASE,BOX,4,L1,2026-06-01,,unpack",
        ])
        self.fixture.add("stock.csv", [
            "2,L1,,2026-09-01,CASE,XC,2",
            "3,L1-A,L1,2026-09-02,BOX,XB,8",
        ])
        self.fixture.add("stock_moves.csv", ["3,OP1,L1,2,L1-A"])
        self.fixture.add("relabel.csv", [
            "5,A1,L1-A,hold,XB,8",
            "7,A2,L1-A,confirmed,XB,6",
            "7,A3,L1-A,hold,XB,2",
        ])
        _b, _c, inventory, _e = self.build()
        ledger = inventory.get("L1-A")
        self.assertEqual(ledger.confirmed_for("XB"), 6)
        self.assertEqual(ledger.hold_for("XB"), 2)  # 不是 8+2
        self.assertIn(BLOCK_LABEL_PENDING, ledger.block_reasons)

    # ---- 可售判定与渠道占用 -----------------------------------------

    def _box_case_fixture(self):
        """一个外箱两种子包装 + 多渠道的标准场景。"""
        self.fixture.add("units.csv", [
            "1,S1,CASE,BOX-A,4,L1,2026-06-01,,unpack",
            "1,S1,CASE,BOX-B,2,L1,2026-06-01,,unpack",
        ])
        self.fixture.add("stock.csv", [
            "2,L1,,2026-09-01,CASE,XC,10",
            "3,L1-A,L1,2026-09-02,BOX-A,XA,16",
            "3,L1-B,L1,2026-09-02,BOX-B,XB,8",
        ])
        self.fixture.add("stock_moves.csv", ["3,OP1,L1,4,L1-A;L1-B"])
        self.fixture.add("channel.csv", [
            "2,wholesale,V1,P,CASE,XC,1,6",
            "3,retail,V2,P,BOX-A,XA,2,10",
            "3,local,V3,P,BOX-B,XB,3,8",
        ])

    def test_unpack_only_cannot_be_sold_as_child_units(self):
        self._box_case_fixture()
        _b, _c, _i, engine = self.build()
        evaluation = engine.evaluate()
        # 整箱余 6；盒只来自已拆派生批次，未拆箱不得折算成盒
        v2 = evaluation["V2"]
        self.assertEqual(v2.state, LINE_ACTIVE)
        self.assertEqual(v2.committable_units, 10)
        reasons = {bucket.reason for bucket in v2.supply}
        self.assertNotIn("identical_conversion", reasons)
        # CASE 行的供给里保留 unpack 仅供解包的事实
        v1 = evaluation["V1"]
        self.assertEqual(v1.committable_units, 6)

    def test_channel_rank_orders_commitments_without_double_booking(self):
        self._box_case_fixture()
        # wholesale(rank1) 先锁定 6 箱；retail 的盒只能用已拆的 16 个
        self.fixture.add("receipt.csv", ["4,wholesale,V1,P1,claimed,L1:6"])
        _b, _c, _i, engine = self.build()
        evaluation = engine.evaluate()
        self.assertEqual(evaluation["V1"].state, LINE_CLAIMED)
        locked_lots = engine.locked_lots()
        self.assertEqual(locked_lots.get("L1"), 6)
        # L1 余 4 整箱仍是 CASE 池，盒行拿不到这 4 箱的折算量
        v2_supply = {bucket.lot: bucket for bucket in evaluation["V2"].supply}
        self.assertNotIn("L1", v2_supply)

    def test_released_line_cannot_republish_stock_as_available(self):
        self._box_case_fixture()
        self.fixture.add("receipt.csv", [
            "4,wholesale,V1,P1,claimed,L1:6",
            "6,wholesale,V1,P1,released,L1:6",
        ])
        _b, _c, _i, engine = self.build()
        evaluation = engine.evaluate()
        self.assertEqual(evaluation["V1"].state, LINE_RELEASED)
        self.assertEqual(evaluation["V1"].committable_units, 0)
        self.assertEqual(evaluation["V1"].locked_units, 0)

    def test_rejected_line_stays_rejected(self):
        self._box_case_fixture()
        self.fixture.add("receipt.csv", ["5,retail,V2,P2,rejected,"])
        _b, _c, _i, engine = self.build()
        self.assertEqual(engine.evaluate()["V2"].state, LINE_REJECTED)

    def test_unresolved_subcomponent_blocks_case_quote_but_sibling_survives(self):
        self._box_case_fixture()
        self.fixture.add("units.csv", [
            "5,S1,CASE,BOX-A,4,L1,2026-06-01,,unresolved",
        ])
        _b, _c, inventory, engine = self.build()
        self.assertIn(BLOCK_SUBCOMPONENT_UNRESOLVED, inventory.get("L1").block_reasons)
        evaluation = engine.evaluate()
        # 整箱报价受阻；独立贴签的 BOX-B 派生批次仍可继续
        self.assertIn(evaluation["V1"].state, (LINE_BLOCKED, LINE_SHORT))
        v3 = evaluation["V3"]
        self.assertEqual(v3.state, LINE_ACTIVE)
        self.assertEqual(v3.committable_units, 8)

    def test_held_labels_reduce_shipable_but_keep_physical_quantity(self):
        self._box_case_fixture()
        self.fixture.add("relabel.csv", [
            "5,A1,L1-A,hold,XA,16",
            "6,A2,L1-A,confirmed,XA,10",
            "6,A3,L1-A,hold,XA,6",
        ])
        _b, _c, _i, engine = self.build()
        evaluation = engine.evaluate()
        v2 = evaluation["V2"]
        # 实物 16 不变，但只有 10 个 confirmed 可承诺
        self.assertEqual(v2.line and v2.committable_units, 10)
        self.assertIn(REASON_HOLD, {bucket.reason for bucket in v2.supply})

    def test_short_line_when_supply_below_demand(self):
        self.fixture.add("units.csv", [
            "1,S1,CASE,BOX,4,L1,2026-06-01,,identical",
        ])
        self.fixture.add("stock.csv", ["2,L1,,2026-09-01,CASE,XC,2"])
        self.fixture.add("channel.csv", ["2,ch,V1,P,CASE,XC,1,5"])
        _b, _c, _i, engine = self.build()
        self.assertEqual(engine.evaluate()["V1"].state, LINE_SHORT)

    def test_label_gap_state_for_unlabeled_stock(self):
        self.fixture.add("stock.csv", ["2,L1,,2026-09-01,CASE,,4"])
        self.fixture.add("channel.csv", ["2,ch,V1,P,CASE,XC,1,4"])
        _b, _c, _i, engine = self.build()
        self.assertEqual(engine.evaluate()["V1"].state, LINE_LABEL_GAP)

    # ---- 人工圈定与追溯 ---------------------------------------------

    def test_disapply_rejudges_parent_and_derived_lots(self):
        self._box_case_fixture()
        _b, claims, inventory, engine = self.build()
        overrides = OverrideBook(self.home).pairs()
        self.assertEqual(overrides, set())
        OverrideBook(self.home).disapply("S1", "L1", "厂家通知", 7)
        view = apply_overrides_to_claims(claims, {("S1", "L1")})
        edge = view.edge("S1", "CASE", "BOX-A", "L1")
        self.assertEqual(edge.window_state, DISAPPLY_OVERRIDE)
        rebuilt_inventory = Inventory(load_all(self.home), view)
        self.assertIn(BLOCK_SUBCOMPONENT_UNRESOLVED,
                      rebuilt_inventory.get("L1").block_reasons)
        # 实物量不变
        self.assertEqual(rebuilt_inventory.get("L1").on_hand_units,
                         inventory.get("L1").on_hand_units)
        self.assertEqual(rebuilt_inventory.get("L1-A").on_hand_units, 16)

    def test_trace_finds_lots_receipts_and_disposition(self):
        self._box_case_fixture()
        self.fixture.add("receipt.csv", [
            "4,retail,V2,P2,claimed,L1-A:10",
        ])
        self.fixture.add("relabel.csv", [
            "6,A9,L1-A,hold,XA,6",
        ])
        batches, claims, inventory, engine = self.build()
        tracer = Tracer(batches, claims, inventory, engine)
        result = tracer.trace("V2")
        self.assertEqual(result.channel_decision, "claimed")
        self.assertEqual(result.disposition, DISPOSITION_RELABEL)
        lots = {item.lot: item for item in result.fulfillment}
        self.assertIn("L1-A", lots)
        self.assertTrue(any(impact.kind == "label_hold" for impact in result.impacts))

    def test_trace_released_line_requires_pause(self):
        self._box_case_fixture()
        self.fixture.add("receipt.csv", [
            "4,wholesale,V1,P1,claimed,L1:6",
            "6,wholesale,V1,P1,released,L1:6",
        ])
        batches, claims, inventory, engine = self.build()
        result = Tracer(batches, claims, inventory, engine).trace("V1")
        self.assertEqual(result.disposition, DISPOSITION_PAUSE)
        self.assertTrue(any(impact.kind == "receipt_released"
                            for impact in result.impacts))

    # ---- 版本化发布 -------------------------------------------------

    def test_publish_is_replayable_and_versioned(self):
        self._box_case_fixture()
        ingest_dir(self.home, self.source)
        pubs = Publisher(self.home).publish_all()
        versions = sorted(path.name for path in (self.home / "published").iterdir()
                          if path.name.startswith("v"))
        self.assertEqual(versions, ["v001", "v002", "v003"])
        current = read_jsonl(self.home / "published" / "current" / "catalog_lines.jsonl")
        self.assertEqual({row["line"] for row in current}, {"V1", "V2", "V3"})
        # 重新发布结果一致（可重建）
        import shutil
        shutil.rmtree(self.home / "published")
        Publisher(self.home).publish_all()
        again = read_jsonl(self.home / "published" / "current" / "catalog_lines.jsonl")
        self.assertEqual(
            json.dumps(current, sort_keys=True, ensure_ascii=False),
            json.dumps(again, sort_keys=True, ensure_ascii=False))
        self.assertTrue(pubs)


if __name__ == "__main__":
    unittest.main()
