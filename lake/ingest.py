"""交接资料导入：原件落地、结构校验与规范化。

设计约定：
- 原始层不可变：原件按交接批次原样复制到 lake_home/raw 下并记录哈希；
- 整批原子：任一文件校验失败，本批次不写入原始层；
- 可重复导入：同批次同哈希再次导入直接跳过，内容变更必须显式替换；
- 校验问题一次性收集抛出，避免修一条报一条。
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    LABEL_ACTIONS,
    PackageEdge,
    ChannelLine,
    IngestedBatch,
    MoveRecord,
    ReceiptRecord,
    RelabelRecord,
    RECEIPT_STATES,
    SCOPES,
    StockRecord,
    parse_day,
)

DATA_FILES = (
    "units.csv",
    "stock.csv",
    "stock_moves.csv",
    "channel.csv",
    "receipt.csv",
    "relabel.csv",
)


class IngestError(ValueError):
    """校验失败时抛出，issues 中保留全部问题。"""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("；".join(issues))


def read_rows(path: Path) -> list[dict[str, str]]:
    """读取 CSV/TSV/JSONL 交接文件并做基础表头检查。"""
    with path.open(encoding="utf-8", newline="") as stream:
        if path.suffix == ".jsonl":
            rows = [json.loads(line) for line in stream if line.strip()]
            if rows and not all(isinstance(row, dict) for row in rows):
                raise IngestError([f"文件包含非对象记录：{path.name}"])
            return [{key: "" if value is None else str(value) for key, value in row.items()}
                    for row in rows]
        delimiter = "\t" if path.suffix == ".tsv" else ","
        reader = csv.DictReader(stream, delimiter=delimiter)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise IngestError([f"文件表头无效：{path.name}"])
        rows = list(reader)
        if any(None in row or None in row.values() for row in rows):
            raise IngestError([f"记录列数与表头不符：{path.name}"])
        return rows


def _int(issues: list[str], row: dict[str, str], key: str, context: str,
         *, minimum: int = 0) -> int | None:
    raw = (row.get(key) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        issues.append(f"{context} 的 {key} 不是整数：{raw!r}")
        return None
    if value < minimum:
        issues.append(f"{context} 的 {key} 小于最小值 {minimum}：{value}")
    return value


def _arrival(row: dict[str, str]) -> int:
    raw = (row.get("arrival") or "").strip()
    return int(raw) if raw else 0


def _parse_batch(arrival: int, rows_by_file: dict[str, list[dict[str, str]]]) -> IngestedBatch:
    """把一个交接批次的原始行转为规范化记录，收集本批校验问题。"""
    issues: list[str] = []
    batch = IngestedBatch(arrival=arrival)

    for row in rows_by_file.get("units.csv", []):
        ctx = f"units.csv 声明 {row.get('claim')} {row.get('package')}->{row.get('child')}"
        factor = _int(issues, row, "factor", ctx, minimum=1)
        scope = (row.get("scope") or "").strip()
        if scope not in SCOPES:
            issues.append(f"{ctx} 的 scope 非法：{scope!r}")
        try:
            valid_from = parse_day(row["from"])
            if valid_from is None:
                issues.append(f"{ctx} 缺少生效起始日 from")
                valid_from = parse_day("1900-01-01")
            edge = PackageEdge(
                source_arrival=arrival, source_file="units.csv",
                claim=(row.get("claim") or "").strip(),
                package=(row.get("package") or "").strip(),
                child=(row.get("child") or "").strip(),
                factor=factor or 0, lot=(row.get("lot") or "").strip(),
                valid_from=valid_from, valid_until=parse_day(row.get("until") or ""),
                scope=scope,
            )
            batch.edges.append(edge)
        except ValueError:
            issues.append(f"{ctx} 的日期无法解析")

    for row in rows_by_file.get("stock.csv", []):
        ctx = f"stock.csv 批次 {row.get('lot')}"
        units = _int(issues, row, "units", ctx, minimum=1)
        try:
            at = parse_day(row["at"])
            if at is None:
                issues.append(f"{ctx} 缺少收货/翻包日期 at")
                at = parse_day("1900-01-01")
            batch.stock.append(StockRecord(
                source_arrival=arrival, source_file="stock.csv",
                lot=(row.get("lot") or "").strip(),
                origin=(row.get("origin") or "").strip() or None,
                at=at, package=(row.get("package") or "").strip(),
                label=(row.get("label") or "").strip() or None,
                units=units or 0,
            ))
        except ValueError:
            issues.append(f"{ctx} 的日期无法解析")

    for row in rows_by_file.get("stock_moves.csv", []):
        ctx = f"stock_moves.csv 操作 {row.get('move')}"
        used = _int(issues, row, "used", ctx, minimum=1)
        children = tuple(item.strip() for item in (row.get("children") or "").split(";")
                         if item.strip())
        if not children:
            issues.append(f"{ctx} 缺少派生批次 children")
        batch.moves.append(MoveRecord(
            source_arrival=arrival, source_file="stock_moves.csv",
            move=(row.get("move") or "").strip(),
            origin=(row.get("origin") or "").strip(), used=used or 0,
            children=children,
        ))

    for row in rows_by_file.get("channel.csv", []):
        ctx = f"channel.csv 目录行 {row.get('line')}"
        rank = _int(issues, row, "rank", ctx, minimum=1)
        hold = _int(issues, row, "hold_units", ctx, minimum=1)
        if not (row.get("requires_label") or "").strip():
            issues.append(f"{ctx} 缺少标签要求 requires_label")
        batch.channels.append(ChannelLine(
            source_arrival=arrival, source_file="channel.csv",
            channel=(row.get("channel") or "").strip(),
            line=(row.get("line") or "").strip(), sku=(row.get("sku") or "").strip(),
            unit=(row.get("unit") or "").strip(),
            requires_label=(row.get("requires_label") or "").strip(),
            rank=rank or 0, hold_units=hold or 0,
        ))

    for row in rows_by_file.get("receipt.csv", []):
        ctx = f"receipt.csv 目录行 {row.get('line')}"
        state = (row.get("state") or "").strip()
        if state not in RECEIPT_STATES:
            issues.append(f"{ctx} 的回执状态非法：{state!r}")
        allocation: list[tuple[str, int]] = []
        for piece in filter(None, (item.strip() for item in (row.get("allocation") or "").split(";"))):
            if ":" not in piece:
                issues.append(f"{ctx} 的占用明细格式应为 批次:数量：{piece!r}")
                continue
            lot, raw_qty = piece.rsplit(":", 1)
            try:
                qty = int(raw_qty.strip())
            except ValueError:
                issues.append(f"{ctx} 的占用明细数量不是整数：{piece!r}")
                continue
            if qty < 1:
                issues.append(f"{ctx} 的占用明细数量小于 1：{piece!r}")
                continue
            allocation.append((lot.strip(), qty))
        if state == "claimed" and not allocation:
            issues.append(f"{ctx} 的 claimed 回执必须给出 allocation 占用明细")
        batch.receipts.append(ReceiptRecord(
            source_arrival=arrival, source_file="receipt.csv",
            channel=(row.get("channel") or "").strip(),
            line=(row.get("line") or "").strip(),
            publication=(row.get("publication") or "").strip(),
            state=state, allocation=tuple(allocation),
        ))

    for row in rows_by_file.get("relabel.csv", []):
        ctx = f"relabel.csv 证书 {row.get('certificate')}"
        action = (row.get("action") or "").strip()
        if action not in LABEL_ACTIONS:
            issues.append(f"{ctx} 的 action 非法：{action!r}")
        scope_units = _int(issues, row, "scope_units", ctx, minimum=1)
        batch.relabels.append(RelabelRecord(
            source_arrival=arrival, source_file="relabel.csv",
            certificate=(row.get("certificate") or "").strip(),
            lot=(row.get("lot") or "").strip(), action=action,
            label=(row.get("label") or "").strip(),
            scope_units=scope_units or 0,
        ))

    _intra_batch_checks(batch, issues)
    if issues:
        raise IngestError(issues)
    return batch


def _intra_batch_checks(batch: IngestedBatch, issues: list[str]) -> None:
    """单批次内的唯一性与必填检查。"""
    seen: set[tuple] = set()

    def duplicate(key: tuple, label: str) -> None:
        if key in seen:
            issues.append(f"重复记录：{label}")
        seen.add(key)

    for edge in batch.edges:
        duplicate((edge.claim, edge.package, edge.child, edge.lot),
                  f"units.csv {edge.claim} {edge.package}->{edge.child} lot={edge.lot}")
    for rec in batch.stock:
        duplicate(("stock", rec.lot), f"stock.csv lot={rec.lot}")
        if not rec.lot or not rec.package:
            issues.append(f"stock.csv 存在缺少 lot/package 的记录")
    for rec in batch.moves:
        duplicate(("move", rec.move), f"stock_moves.csv move={rec.move}")
    for rec in batch.channels:
        duplicate(("channel", rec.line), f"channel.csv line={rec.line}")
    for rec in batch.receipts:
        duplicate(("receipt", rec.line), f"receipt.csv line={rec.line}")
    for rec in batch.relabels:
        duplicate(("cert", rec.certificate), f"relabel.csv certificate={rec.certificate}")


def cross_check(batches: list[IngestedBatch]) -> list[str]:
    """跨批次引用与勾稽检查；返回问题列表（空列表代表通过）。"""
    issues: list[str]
    issues = []
    stock_by_lot: dict[str, StockRecord] = {}
    edges: list[PackageEdge] = []
    channel_lines: set[str] = set()
    move_used: dict[str, int] = {}

    for batch in batches:
        edges.extend(batch.edges)
        channel_lines.update(line.line for line in batch.channels)
        for rec in batch.stock:
            if rec.lot in stock_by_lot:
                issues.append(f"批次号在多个交接件中重复出现：{rec.lot}")
            stock_by_lot[rec.lot] = rec

    # 翻包派生关系与父批次用量
    for batch in batches:
        for move in batch.moves:
            parent = stock_by_lot.get(move.origin)
            if parent is None:
                issues.append(f"翻包 {move.move} 的父批次不存在：{move.origin}")
                continue
            move_used[move.origin] = move_used.get(move.origin, 0) + move.used
            for child_lot in move.children:
                child = stock_by_lot.get(child_lot)
                if child is None:
                    issues.append(f"翻包 {move.move} 的派生批次不存在：{child_lot}")
                    continue
                if child.origin != move.origin:
                    issues.append(
                        f"派生批次 {child_lot} 的 origin={child.origin} 与翻包 {move.move} 不符")
                match = next((e for e in edges
                              if e.lot == move.origin and e.package == parent.package
                              and e.child == child.package and e.factor
                              and child.units == move.used * e.factor), None)
                if match is None:
                    issues.append(
                        f"翻包 {move.move} 派生 {child_lot} 的数量 {child.units} "
                        f"无法与 {parent.package}->{child.package} 的声明因子勾稽")

    for lot, used in move_used.items():
        parent = stock_by_lot.get(lot)
        if parent and used > parent.units:
            issues.append(f"父批次 {lot} 累计拆用 {used} 超过实收 {parent.units}")

    # 贴签证据：同一批次在同一时点 confirmed 与 hold 各为一份分量，
    # 校验“同时有效”的分量不超过实物量，而不是把历史证书累加
    relabel_latest: dict[tuple[str, str], dict[str, int]] = {}
    for batch in batches:
        for rec in batch.relabels:
            lot = stock_by_lot.get(rec.lot)
            if lot is None:
                issues.append(f"贴签证书 {rec.certificate} 的批次不存在：{rec.lot}")
                continue
            if lot.origin is None:
                issues.append(f"贴签证书 {rec.certificate} 针对的 {rec.lot} 不是翻包派生批次")
            bucket = relabel_latest.setdefault((rec.lot, rec.label),
                                               {"confirmed": 0, "hold": 0})
            bucket[rec.action] = rec.scope_units
    for (lot, label), qty in relabel_latest.items():
        if lot in stock_by_lot:
            for action, total in qty.items():
                if total and total > stock_by_lot[lot].units:
                    issues.append(
                        f"批次 {lot} 标签 {label} 的 {action} 证书范围 {total} "
                        f"超过实物量 {stock_by_lot[lot].units}")
            active_total = qty["confirmed"] + qty["hold"]
            if active_total > stock_by_lot[lot].units:
                issues.append(
                    f"批次 {lot} 标签 {label} 同时有效的 confirmed+hold "
                    f"{active_total} 超过实物量 {stock_by_lot[lot].units}")

    # 回执引用与占用明细
    for batch in batches:
        for rec in batch.receipts:
            if rec.line not in channel_lines:
                issues.append(f"回执引用了未登记的目录行：{rec.line}")
            for lot, qty in rec.allocation:
                target = stock_by_lot.get(lot)
                if target is None:
                    issues.append(f"回执 {rec.line} 占用了不存在的批次：{lot}")
                elif qty > target.units:
                    issues.append(f"回执 {rec.line} 占用 {lot}:{qty} 超过实物量 {target.units}")
    return issues


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _registry_path(lake_home: Path) -> Path:
    return lake_home / "raw" / "registry.json"


def load_registry(lake_home: Path) -> dict:
    lake_home = Path(lake_home)
    path = _registry_path(lake_home)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"arrivals": {}}


def _save_registry(lake_home: Path, registry: dict) -> None:
    path = _registry_path(lake_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")


def ingest_dir(lake_home: Path, source_dir: Path, *, replace: bool = False) -> list[IngestedBatch]:
    """导入一个交接目录下的全部资料，返回新落地的规范化批次列表。

    原件按 arrival 分组后整批校验、整批复制到原始层；已存在的同哈希批次跳过。
    """
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise IngestError([f"交接目录不存在：{source_dir}"])

    present = {path.name: path for path in sorted(source_dir.iterdir())
               if path.is_file() and path.suffix in {".csv", ".tsv", ".jsonl"}}
    unknown = sorted(set(present) - set(DATA_FILES))
    if unknown:
        raise IngestError([f"未识别的交接文件：{name}" for name in unknown])

    grouped: dict[int, dict[str, list[dict[str, str]]]] = {}
    hashes: dict[int, dict[str, str]] = {}
    for name in DATA_FILES:
        if name not in present:
            continue
        rows = read_rows(present[name])
        digest = _file_hash(present[name])
        for row in rows:
            arrival = _arrival(row)
            grouped.setdefault(arrival, {}).setdefault(name, []).append(row)
            hashes.setdefault(arrival, {})[name] = digest

    registry = load_registry(lake_home)
    imported: list[IngestedBatch] = []
    for arrival in sorted(grouped):
        previous = registry["arrivals"].get(str(arrival))
        current_hashes = hashes[arrival]
        if previous is not None:
            if previous["hashes"] == current_hashes and not replace:
                continue
            if not replace:
                raise IngestError([
                    f"批次 {arrival} 已导入且原件哈希不一致；如需覆盖请使用 replace 模式"])
        batch = _parse_batch(arrival, grouped[arrival])
        # 与原始层中既有批次（含本次已落地的新批次）一起做跨批勾稽
        prior_batches = [load_batch(lake_home, int(old_arrival))
                         for old_arrival in sorted(registry["arrivals"], key=int)
                         if int(old_arrival) != arrival]
        issues = cross_check(prior_batches + [batch])
        if issues:
            raise IngestError(issues)

        raw_dir = lake_home / "raw" / f"arrival_{arrival:03d}"
        if replace and raw_dir.exists():
            shutil.rmtree(raw_dir)
        raw_dir.mkdir(parents=True, exist_ok=False)
        for name in grouped[arrival]:
            shutil.copyfile(present[name], raw_dir / name)
        manifest = {
            "arrival": arrival,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "hashes": current_hashes,
            "counts": {
                "edges": len(batch.edges), "stock": len(batch.stock),
                "moves": len(batch.moves), "channels": len(batch.channels),
                "receipts": len(batch.receipts), "relabels": len(batch.relabels),
            },
        }
        (raw_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (raw_dir / "batch.json").write_text(
            json.dumps(asdict(batch), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        registry["arrivals"][str(arrival)] = {
            "raw_dir": str(raw_dir.relative_to(lake_home)), "hashes": current_hashes}
        imported.append(batch)

    _save_registry(lake_home, registry)
    return imported


def load_batch(lake_home: Path, arrival: int) -> IngestedBatch:
    """从原始层读取一个已导入批次的规范化记录。"""
    raw = json.loads((lake_home / "raw" / f"arrival_{arrival:03d}" / "batch.json")
                     .read_text(encoding="utf-8"))

    def day(value: str | None):
        return parse_day(value) if value else None

    return IngestedBatch(
        arrival=raw["arrival"],
        edges=[PackageEdge(**{**e, "valid_from": day(e["valid_from"]),
                              "valid_until": day(e["valid_until"])}) for e in raw["edges"]],
        stock=[StockRecord(**{**s, "at": day(s["at"]),
                              "origin": s["origin"] or None, "label": s["label"] or None})
               for s in raw["stock"]],
        moves=[MoveRecord(**{**m, "children": tuple(m["children"])}) for m in raw["moves"]],
        channels=[ChannelLine(**c) for c in raw["channels"]],
        receipts=[ReceiptRecord(**{**r, "allocation": tuple(map(tuple, r["allocation"]))})
                  for r in raw["receipts"]],
        relabels=[RelabelRecord(**r) for r in raw["relabels"]],
    )


def load_all(lake_home: Path) -> list[IngestedBatch]:
    """按交接顺序读取原始层中的全部批次。"""
    lake_home = Path(lake_home)
    registry = load_registry(lake_home)
    return [load_batch(lake_home, int(arrival))
            for arrival in sorted(registry["arrivals"], key=int)]
