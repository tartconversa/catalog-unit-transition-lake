"""数据湖统一领域模型。

所有记录都保留来源证据：source_arrival（交接批次号）与 source_file
（原始文件名），保证任何发布行都能回溯到交接原件。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# 包装关系适用范围（对照三态）
SCOPE_IDENTICAL = "identical"      # 等价：可按子包装单位直接履约
SCOPE_UNPACK = "unpack"            # 仅供解包换算：可记录翻包，不代表允许拆售
SCOPE_UNRESOLVED = "unresolved"    # 不确定：任何依赖该关系的报价均需人工判定
SCOPES = (SCOPE_IDENTICAL, SCOPE_UNPACK, SCOPE_UNRESOLVED)

# 回执状态
RECEIPT_CLAIMED = "claimed"
RECEIPT_RELEASED = "released"
RECEIPT_REJECTED = "rejected"
RECEIPT_STATES = (RECEIPT_CLAIMED, RECEIPT_RELEASED, RECEIPT_REJECTED)

# 翻包证书动作
LABEL_HOLD = "hold"
LABEL_CONFIRMED = "confirmed"
LABEL_ACTIONS = (LABEL_HOLD, LABEL_CONFIRMED)

# 人工圈定声明不适用时，声明关系在台账窗口状态中的标记
DISAPPLY_OVERRIDE = "claim_disapplied"


def parse_day(value: str) -> date | None:
    """解析交接资料中的日期；空串视为持续有效（None）。"""
    value = (value or "").strip()
    if not value:
        return None
    return date.fromisoformat(value)


@dataclass(frozen=True)
class PackageEdge:
    """厂家声明中的一条有向包装关系（父包装 -> 子包装）。"""

    source_arrival: int
    source_file: str
    claim: str
    package: str          # 父包装
    child: str            # 子包装
    factor: int           # 每个父包装含有的子包装数量
    lot: str              # 适用生产批次
    valid_from: date
    valid_until: date | None
    scope: str

    def is_active_on(self, day: date) -> bool:
        """该关系在指定出厂日是否处于声明有效期内（左闭右开）。"""
        if day < self.valid_from:
            return False
        return self.valid_until is None or day < self.valid_until


@dataclass(frozen=True)
class StockRecord:
    """stock.csv 一行：origin 为空是实收，否则为翻包派生批次。"""

    source_arrival: int
    source_file: str
    lot: str
    origin: str | None
    at: date
    package: str
    label: str | None
    units: int


@dataclass(frozen=True)
class MoveRecord:
    """stock_moves.csv 一行：父批次被实际拆用的数量与派生批次。"""

    source_arrival: int
    source_file: str
    move: str
    origin: str
    used: int
    children: tuple[str, ...]


@dataclass(frozen=True)
class ChannelLine:
    """channel.csv 一行：渠道对某目录行、某包装单位的接单规格。"""

    source_arrival: int
    source_file: str
    channel: str
    line: str
    sku: str
    unit: str
    requires_label: str
    rank: int
    hold_units: int


@dataclass(frozen=True)
class ReceiptRecord:
    """receipt.csv 一行：目录行的认领、释放或拒绝回执及占用明细。"""

    source_arrival: int
    source_file: str
    channel: str
    line: str
    publication: str
    state: str
    allocation: tuple[tuple[str, int], ...]  # (批次, 数量) 明细


@dataclass(frozen=True)
class RelabelRecord:
    """relabel.csv 一行：某派生批次实物范围内的贴签证据。"""

    source_arrival: int
    source_file: str
    certificate: str
    lot: str
    action: str
    label: str
    scope_units: int


@dataclass(frozen=True)
class IngestedBatch:
    """一次交接批次（arrival）导入后的完整规范化记录集合。"""

    arrival: int
    edges: list[PackageEdge] = field(default_factory=list)
    stock: list[StockRecord] = field(default_factory=list)
    moves: list[MoveRecord] = field(default_factory=list)
    channels: list[ChannelLine] = field(default_factory=list)
    receipts: list[ReceiptRecord] = field(default_factory=list)
    relabels: list[RelabelRecord] = field(default_factory=list)
