# r1 → r2 逐条差异（576 时点，同一份清单）

- 行数：r1 576 → r2 576（仅 r1 有 0，仅 r2 有 0）
- 变化行数：215
- selected：r1 229 → r2 224
- r1 中「未评分却 selected」：6 → r2 0
- K 线状态 r1：`{'kline_gap': 91, 'kline_short_history': 78, 'no_kline_data': 41, 'ok': 366}`
- K 线状态 r2：`{'kline_gap': 91, 'kline_short_history': 75, 'no_kline_data': 41, 'ok': 369}`
- 合约状态 r1：`{'failed': 175, 'no_futures': 48, 'ok': 192, 'partial': 161}`
- 合约状态 r2：`{'failed': 127, 'no_futures': 48, 'ok': 240, 'partial': 161}`
- 去向（r2）：`{'scored_below_threshold': 145, 'selected': 224, 'skipped:kline_gap': 91, 'skipped:kline_short_history': 75, 'skipped:no_kline_data': 41}`
- 采用点≠decision−1h：r1 1 → r2 1

## 字段变化计数

| 字段 | 变化行数 |
|---|---:|
| kline_status | 3 |
| kline_score | 3 |
| score_kline | 208 |
| score_deriv | 0 |
| score_total | 208 |
| selected | 5 |
| deriv_status | 48 |

## 变化行明细（最多 60 条，完整见 diff_vs_r1.json）

| 段 | 标的 | 决策(UTC) | 变化字段 | before → after |
|---|---|---|---|---|
| S03 | KLAYUSDT | 2024-10-28T00:00:00Z | deriv_status | deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T01:00:00Z | deriv_status | deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T02:00:00Z | deriv_status | deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T03:00:00Z | deriv_status | deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T04:00:00Z | deriv_status | deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T05:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T06:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T07:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T08:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T09:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T10:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T11:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T12:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T13:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T14:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T15:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T16:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T17:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T18:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T19:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T20:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T21:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T22:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-28T23:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T00:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T01:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T02:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T03:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T04:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T05:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T06:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T07:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T08:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T09:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T10:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T11:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T12:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T13:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T14:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T15:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T16:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T17:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T18:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T19:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T20:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T21:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T22:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S03 | KLAYUSDT | 2024-10-29T23:00:00Z | score_kline; score_total; deriv_status | score_kline: 0→None; score_total: 0→None; deriv_status: failed→ok |
| S05 | WIFUSDT | 2024-03-05T00:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 0→None |
| S05 | WIFUSDT | 2024-03-05T01:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 0→None |
| S05 | WIFUSDT | 2024-03-05T02:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 3→None |
| S05 | WIFUSDT | 2024-03-05T03:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 3→None |
| S05 | WIFUSDT | 2024-03-05T04:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 3→None |
| S05 | WIFUSDT | 2024-03-05T05:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 0→None |
| S05 | WIFUSDT | 2024-03-05T06:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 0→None |
| S05 | WIFUSDT | 2024-03-05T07:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 0→None |
| S05 | WIFUSDT | 2024-03-05T08:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 0→None |
| S05 | WIFUSDT | 2024-03-05T09:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 0→None |
| S05 | WIFUSDT | 2024-03-05T10:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 3→None |
| S05 | WIFUSDT | 2024-03-05T11:00:00Z | score_kline; score_total | score_kline: 0→None; score_total: 3→None |
