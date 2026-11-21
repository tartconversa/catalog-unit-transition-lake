# 商品包装规格迁移与可售目录数据湖

回答两个卖场问题——**这次报价指的是哪种包装？现有货里有多少确实能按这种单位发出？**
并在厂家换包装、声明修订、人工圈定不适用、渠道认领/释放/拒绝的持续交接中，
输出渠道可核对的版本化目录行与库存承诺。

## 业务规则落点

| 规则 | 实现位置 |
| --- | --- |
| 同一货号多包装（盒/外箱），声明按生产批次与出厂日适用，区间左闭右开 | `lake/catalog.py`、`lake/models.py` |
| 一个外箱含两种子包装，修订只调整明确给出的子类，其余沿用 | `ClaimBook` 修订合并 |
| 对照三态：`identical` 可按子单位履约 / `unpack` 仅供解包 / `unresolved` 阻断 | `ResolvedEdge` |
| 仅有可换算数量不代表允许拆售；已拆盒与未拆箱分池记账 | `lake/inventory.py`、`lake/availability.py` |
| 收货与翻包分别记账，父子不重复计算；标签只认 confirmed/hold 证书 | `Inventory` 台账 |
| 批次标签暂缺时只给已证实实物量，不默认按当前目录 | `BLOCK_LABEL_MISSING` |
| 人工圈定声明不适用：重判余货与已拆派生批次，实物量不变、可出货量减少 | `lake/trace.py` `OverrideBook` |
| 渠道规格需同时满足折算资格、标签要求、库存可按渠道约定处理 | `AvailabilityEngine` |
| 带锁定供给按 rank（同级按渠道号）顺序占用，整箱与盒不双承诺 | `evaluate()` 贪心占用 |
| 渠道可接管新版行、拒绝报价、确认旧版下架；释放库存不得被新渠道写成可用 | 回执状态机 |
| 已交接报价沿目录行查回原批次/单位、受影响新行与处置依据 | `Tracer.trace` |
| 每日导入生产、翻包、渠道规格、回执，发布版本化行数据与承诺 | `lake/publish.py` |

## 目录结构

```
lake/
  models.py       领域模型与枚举（全部带来源证据字段）
  ingest.py       交接件落地、整批校验、哈希登记、不可变原始层
  catalog.py      声明修订合并、有效期、换算资格
  inventory.py    批次台账：实物量、标签分量、翻包派生、受阻原因
  availability.py 目录行可售判定、渠道顺序占用与互斥分池
  trace.py        影响追溯、人工不适用圈定、处置建议
  publish.py      按交接批次重放，发布版本化 JSONL
  cli.py          命令行入口
tools/
  run_lake.py     独立模式（python -I）引导脚本
  run_tests.py    全量测试引导
  source_inventory.py  原始交接件结构检查（既有）
tests/
  test_lake.py        规则级单元测试（内联最小夹具）
  test_end_to_end.py  examples/ 全量业务事实核对
```

数据湖目录（默认 `lake_home/`）：

```
lake_home/
  raw/arrival_001/...   每个交接批次的原件副本、manifest、规范化 batch.json
  raw/registry.json     原件哈希登记，防篡改、支持幂等导入
  overrides/            人工圈定声明不适用的登记
  published/v001/...    逐版本 catalog_lines / lot_ledger / commitments
  published/current/    指向最新版本的符号链接，渠道实际读取位置
```

## 每日操作

所有命令以独立模式运行，避免不可信交接目录中的模块注入：

```bash
# 1. 导入当天交接目录（可含一个或多个交接文件），自动整批校验并发布
python -I tools/run_lake.py --lake lake_home ingest path/to/todays_exchange

# 2. 重新发布全部版本（发布层可随时从原始层重建）
python -I tools/run_lake.py --lake lake_home publish

# 3. 人工圈定某声明对某批次不适用（自指定交接批次起生效并重判）
python -I tools/run_lake.py --lake lake_home disapply S19 L02 \
    --arrival 7 --reason "厂家通知S19对L02不适用"

# 4. 追溯一条已交接目录行
python -I tools/run_lake.py --lake lake_home trace V21

# 5. 当前台账与目录行汇总
python -I tools/run_lake.py --lake lake_home status
```

## 渠道读取的发布文件

- `catalog_lines.jsonl`：每行含状态（active/short/blocked/label_gap/claimed/
  claimed_at_risk/rejected/released）、锁定量、可承诺量、待确认标签量、
  未证实量、阻断原因与证据链；
- `lot_ledger.jsonl`：每批次实物量、已拆量、在手量、标签 confirmed/hold/缺失
  分量、适用声明与受阻原因；
- `commitments.jsonl`：`locked`（回执真实承诺）与 `committable`（按渠道顺序
  登记的建议占用）两类，批次池互斥。

## 校验与测试

```bash
python -I tools/run_tests.py
```

导入校验包括：表头与列数、枚举值、整数与日期、同批唯一性、翻包数量与声明
因子勾稽、父批次拆用上限、贴签证书范围、回执引用与占用上限。任一问题都会
整批拒绝并一次性返回全部问题，原始层不写入部分数据。

运行环境 Python 3.14，仅依赖标准库。
