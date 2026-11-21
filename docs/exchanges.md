# 交接资料说明

`units.csv` 描述有向包装关系，`factor` 是每个父包装中含有的子包装数量，`scope` 可为 `identical`、`unpack`、`unresolved`。同一父包装和声明号可包含多个子类。区间为左闭右开，空结束表示持续有效。 `stock.csv` 的 `origin` 为空时是实收，否则为父批次实际翻包记录。以 `stock_moves.csv` 的 `used` 记录实际占用的父包装数量，子批次分开记入，避免父子重复计算。 `channel.csv` 的 `requires_label` 是可接单规格，`rank` 数字越小越先，`hold_units` 是本次目录需要承诺的量，相同优先级按渠道号。 `receipt.csv` 保存已有目录行的领取和释放证据，库存分配以 `allocation` 给出的明细为准。同声明中的修订仅调整明确给出的子包装，其余子包装按有效声明。缺任一子组件资格时，整个外箱的商品报价也可能不可继续，但已独立贴签且供应商仍确认的其他子类可继续。relabel 表的数量是该子批次的实物范围，最后一批中 confirmed 与 hold 为同时有效的两份分量。

字段 `arrival` 为合成交接批号，同号记录属于同次接收。没有批号的文件为该批资料采用的固定目录。

- `units.csv`
- `stock.csv`
- `stock_moves.csv`
- `channel.csv`
- `receipt.csv`
- `relabel.csv`
