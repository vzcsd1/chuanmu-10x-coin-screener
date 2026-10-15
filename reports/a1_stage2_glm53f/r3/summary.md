# glm r3 交付摘要（M2 返修 + M0 独立核对 + M1 复验）

**目录**：`reports/a1_stage2_glm53f/r3/`　r2 产物保留未动　全程本地离线。

## 正文（说人话）

主代理点名的五类错我全修了，方法就一句话：**每个数都从原始 ZIP 现场重算，两边不共用任何中间值**。过程中还抓出两个新 bug——最险的是前向窗锚点差一根，期末收盘整体错位一小时，被「随机抽样 300 行独立重算」逼出来；微秒时间戳逐行归一不彻底，2025 年后数据差点被静默漏扫。修复后的账：参考时刻严格 t+1h（缺就标缺，绝不跳根），缺口记正数，期末缺价不补，单边上涨回撤=0，27 个冒充上市期的案例全部消除。旧 841 事件 707 个对上（含此前被漏的 2 个中文币），134 个未命中全部与我某段区间重叠——像「同一条鱼两次过秤」，不是漏网。ds 的 r3 我也独立验过：排序窗口修复从原始合约数据六位小数对上，旧口径会让 ARB 成交额虚高 31%。两包现在可以互相盖章了。

## 逐项明细

| 任务书条目 | 结果 | 证据 |
|---|---|---|
| §2 M0 来源身份（独立核对） | 713 来源=磁盘 713，双向零差；币安人生 6,394 根、牛来 514 根可读；1h 重复 2,234（JUV 2,232+EDU 2）、冲突 0；**新发现日线重复 3 条（AXS/UMA/USDP 各 1）** | `detail/m0_verify_summary.json`、`m0_hourly_dup_scan.csv`、`m0_daily_dup_scan.csv`、`m0_source_identity_glm.csv`（脚本 `scripts/m0_verify_glm.py`） |
| §4.1 参考 t+1h | 事件参考 15,797 整点命中、5 缺失显式标注、0 跳根；SEI 2023-08-17 01:00 open=0.2021（评审 8/8 例通过） | `consistency_r3.json` C1/C2 |
| §4.2 缺口/期末 | 缺口正计数（BTC 反例 gap=1）；期末缺价 NaN 不填补（30d 反例）；尾部不足标 immature | C3；`fixed_outcomes_stats.json`：gap30 正数行 254,991、immature30 3.13% |
| §4.3 回撤先后 | 合成单边 1→1.2→1.5→2→3→4 回撤=0、不回踩=True；30%/50% 仍只作诊断（10,703 / 5,099 事件，不选赢家） | C5；`events_diag_30.csv`、`events_fixed_50.csv` |
| §4.4 上市标签/BIO | 评审 27 例反例残留 0（156→131）；BIO 首根 bar 内 27~49 倍先后未知，次日进场剩余仅 1.063x | 反例比对 0/54 残留；`detail/bio_same_day_annex.json`（可复跑 `scripts/bio_annex_r3.py`） |
| §4.5 独立一致性 | C1~C7 **ALL_PASS**：自带原始解析器重算参考价/最高/最低/期末/缺口，底表抽样 300 行重算比对（33 万次断言级比对） | `detail/consistency_r3.json`、`scripts/m2_consistency_r3.py` |
| §4.6 旧 841 固化 | 707 命中30%（含中文币 2 条）、134 未命中全部区间重叠、完全不重叠 0；十倍级漏配对仅 TRB 11.2x/SUPER 9.6x；缺月 5 币含 **CVC**（订正「全为稳定币/FTT」） | `detail/legacy841_match_r3.csv`、`legacy841_miss_attribution_r3.csv`、`daily_gap_months_r3.csv`（脚本 `scripts/legacy841_r3.py`） |
| §3 M1 独立复验 ds r3 | fqv24 已闭合窗六位有效数字 4/4 对上（HEMI 2.238501e8 等）；旧口径差异 ARB +31.3%、0G +9.9%；中断反例 r3=1 事件 vs r2=2 假新；B6 抽 5 身份 5/5 过 | `m1_verification.md` |
| 底表 | 15,976,661 行 = M0 唯一小时数（严丝合缝）；参考可用 99.993%；30d 窗 ≥2x 占 5.15%、≥3x 1.27%；90d ≥10x 0.24% | `detail/fixed_outcomes_hourly.parquet`、`fixed_outcomes_stats.json` |

## 运行命令（PowerShell，项目根，自退出、无网络）

```powershell
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/m0_verify_glm.py
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/m2_fixed_outcomes_r3.py
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/m2_natural_paths_r3.py
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/m2_consistency_r3.py
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/legacy841_r3.py
py -3.13 -B -u reports/a1_stage2_glm53f/r3/scripts/bio_annex_r3.py
```

## 运行前后摘要

- 输入：`queue_v3.csv`（1,000,272 行）+ `data/archive_raw/`（22,180 份 1h ZIP + 22,180 份 1d ZIP + 合约/其他，均只读）。
- 输出行数：底表 15,976,661；事件表 10,703 / 5,099；身份表 44,360 行（1h+1d 月文件）。
- 代码指纹：交接时以 `ready_for_review.md` 附的清单为准；r2 与原始库零改动。

## 未决项 / 应入账事项

1. **请主代理入账**：日线重复 3 条（AXS/UMA/USDP，内容相同 0 冲突）——ds 的 M0 只核了小时线。
2. **请 ds 复验我的 r3**（尤其 §4.1 参考时刻与 §4.5 一致性范围）；`ready_for_review.md` 已备。
3. 名单关注成本与前 N 曲线：M1 已验合格，等两包交叉确认后用 ds r3 最终名单计算（我的下一交付，不阻塞本轮验收）。
4. B8 界面隔离 demo：按原卡待研究交接窗口推进，绝不真实查询。
5. M3 仍按约定等两包交叉合格后自动接续。
