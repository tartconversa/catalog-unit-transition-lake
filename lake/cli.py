"""数据湖命令行入口。

用法（在项目根目录）：
  python -m lake.cli ingest <交接目录> [--lake 数据湖目录] [--replace]
  python -m lake.cli publish
  python -m lake.cli disapply <声明号> <批次号> --arrival <生效交接批次> --reason <原因>
  python -m lake.cli trace <目录行号>
  python -m lake.cli status

所有命令仅依赖标准库；读取不可信交接件时由调用方以 `python -I` 启动。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .availability import AvailabilityEngine
from .catalog import ClaimBook
from .ingest import IngestError, ingest_dir, load_all, load_registry
from .inventory import Inventory
from .publish import Publisher, build_snapshot, default_as_of
from .trace import OverrideBook, Tracer, apply_overrides_to_claims


def _load_state(lake_home: Path):
    batches = load_all(lake_home)
    overrides = OverrideBook(lake_home).pairs()
    claims = apply_overrides_to_claims(ClaimBook(batches), overrides) if overrides \
        else ClaimBook(batches)
    inventory = Inventory(batches, claims)
    engine = AvailabilityEngine(batches, claims, inventory)
    return batches, claims, inventory, engine


def cmd_ingest(args: argparse.Namespace) -> int:
    try:
        imported = ingest_dir(args.lake, args.source, replace=args.replace)
    except IngestError as exc:
        print(json.dumps({"status": "rejected", "issues": exc.issues},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    if not imported:
        print(json.dumps({"status": "unchanged",
                          "arrivals": sorted(load_registry(args.lake)["arrivals"], key=int)},
                         ensure_ascii=False, indent=2))
        return 0
    publications = Publisher(args.lake).publish_all()
    print(json.dumps({
        "status": "imported",
        "arrivals": [batch.arrival for batch in imported],
        "publications": [asdict(pub) for pub in publications],
    }, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    publications = Publisher(args.lake).publish_all()
    print(json.dumps([asdict(pub) for pub in publications],
                     ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_disapply(args: argparse.Namespace) -> int:
    registry = load_registry(args.lake)
    if str(args.arrival) not in registry["arrivals"]:
        print(f"生效交接批次 {args.arrival} 尚未导入", file=sys.stderr)
        return 2
    override = OverrideBook(args.lake).disapply(
        args.claim, args.lot, args.reason, args.arrival)
    Publisher(args.lake).publish_all()
    print(json.dumps(override.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    batches, claims, inventory, engine = _load_state(args.lake)
    tracer = Tracer(batches, claims, inventory, engine)
    try:
        result = tracer.trace(args.line)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    batches, claims, inventory, engine = _load_state(args.lake)
    as_of = default_as_of(batches) if batches else None
    snapshot = build_snapshot(batches, as_of) if batches else {"lines": [], "lots": []}
    summary = {
        "lake": str(args.lake),
        "arrivals": [batch.arrival for batch in batches],
        "as_of": as_of.isoformat() if as_of else None,
        "lots": [
            {
                "lot": ledger.lot,
                "package": ledger.package,
                "on_hand": ledger.on_hand_units,
                "confirmed": ledger.confirmed_total(),
                "hold": ledger.hold_total(),
                "label_missing": ledger.label_missing_units,
                "block_reasons": list(ledger.block_reasons),
            }
            for ledger in inventory.all_lots()
        ],
        "lines": {
            row["line"]: {
                "channel": row["channel"], "unit": row["unit"],
                "state": row["state"], "locked": row["locked_units"],
                "committable": row["committable_units"],
                "unverified": row["unverified_units"],
                "blockers": row["blockers"],
            }
            for row in snapshot["lines"]
        },
        "current_published": [
            str(path) for path in (args.lake / "published" / "current").glob("*.jsonl")
        ] if (args.lake / "published" / "current").exists() else [],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="包装规格迁移与可售目录数据湖")
    parser.add_argument("--lake", type=Path, default=Path("lake_home"),
                        help="数据湖根目录（默认 lake_home）")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="导入并校验一批交接资料")
    ingest.add_argument("source", type=Path, help="交接资料目录")
    ingest.add_argument("--replace", action="store_true",
                        help="允许覆盖同批次已导入原件（重新落地）")
    ingest.set_defaults(func=cmd_ingest)

    publish = sub.add_parser("publish", help="重放全部交接批次并发布版本化数据")
    publish.set_defaults(func=cmd_publish)

    disapply = sub.add_parser("disapply", help="人工圈定声明对某批次不适用并重判")
    disapply.add_argument("claim")
    disapply.add_argument("lot")
    disapply.add_argument("--arrival", type=int, required=True)
    disapply.add_argument("--reason", default="人工圈定不适用")
    disapply.set_defaults(func=cmd_disapply)

    trace = sub.add_parser("trace", help="追溯一条目录行的批次履约与影响")
    trace.add_argument("line")
    trace.set_defaults(func=cmd_trace)

    status = sub.add_parser("status", help="输出当前台账与目录行汇总")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
