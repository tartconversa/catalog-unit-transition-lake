# 交接资料说明

`units.csv` 描述有向包装关系，`factor` 是每个父包装中含有的子包装数量，`scope` 可为 `identical`、`unpack`、`unresolved`。同一父包装和声明号可包含多个子类。区间为左闭右开，空结束表示持续有效。 `stock.csv` 的 `origin` 为空时是实收，否则为父批次实际翻包记录。以 `stock_moves.csv` 的 `used` 记录实际占用的父包装数量，子批次分开记入，避免父子重复计算。 `channel.csv` 的 `requires_label` 是可接单规格，`rank` 数字越小越先，`hold_units` 是本次目录需要承诺的量，相同优先级按渠道号。 `receipt.csv` 保存已有目录行的领取和释放证据，库存分配以 `allocation` 给出的明细为准。同声明中的修订仅调整明确给出的子包装，其余子包装按有效声明。缺任一子组件资格时，整个外箱的商品报价也可能不可继续，但已独立贴签且供应商仍确认的其他子类可继续。relabel 表的数量是该子批次的实物范围，最后一批中 confirmed 与 hold 为同时有效的两份分量。

字段 `arrival` 为合成交接批号，同号记录属于同次接收。没有批号的文件为该批资料采用的固定目录。

- `units.csv`
- `stock.csv`
- `stock_moves.csv`
- `channel.csv`
- `receipt.csv`
- `relabel.csv`

## 数据湖的处理口径

- 导入按 `arrival` 整批校验、整批落地到不可变原始层（`raw/arrival_xxx/`，含原件哈希），任一条不合格则本批全部不写入。
- `units.csv` 的同声明修订按交接批次合并：修订只替换明确给出的（父包装、子包装、批次）关系，未提及的子包装沿用既有声明。`scope=unresolved` 使整箱报价受阻，但独立贴签且仍被确认的其他子类目录行不受株连。
- 台账区分翻包当时资格（`unpack_time_scope`，按翻包记录所在交接批次还原）与当前资格（`conversion_scopes`）；后续修订为 `unresolved` 不追溯否定已实拆且贴签的派生批次，只阻断未拆外箱的新增拆售。
- `relabel.csv` 同一（标签、动作）的后续证书按最新交接批次取分量，不累加历史；confirmed 与 hold 是同时有效的两份分量，hold 部分实物量不变、可出货量减少。
- `receipt.csv` 的 claimed allocation 是真实锁定，released 后该行不再生成任何可承诺占用，库存须等新渠道回执确认；rejected 行保持拒绝状态。
- 人工圈定声明不适用通过 `python -I tools/run_lake.py disapply <声明> <批次>` 登记在 `overrides/`，重判该批余货与已拆派生批次，并在追溯结果中给出重贴签、换货或暂停接单的处置依据。
- 渠道承诺按 `rank`（同级按渠道号）顺序占用；整箱池与已拆盒池分开，已被认领的外箱不会再被折算成盒承诺给另一渠道。
