# v3 纳管执行手册（`.part` 复用 + 修正清单，一次到位）

日期：2026-10-08
状态：**已就绪，待唯一写入者安全退出后执行**
前置：`reports/archive_download_ds_full/rehearse_v3_reuse.py` 三场景预演全通过

---

## 0. 为什么是"一次纳管"而不是"先修清单重下"

主代理已逐项复算 2,131 份本地文件，全部通过（`local == official != list`）。
所以这 2,131 项**不需要重新下载**——它们的原始字节已完好躺在 `.part` 里。

方案 C（只修清单、走正常下载）会白重下约 24 MB；方案 A 单独用会空转
（旧清单下复用上来的字节仍判异常）。**两者必须一起上**，才能一次纳管：

| 组件 | 作用 | 缺了会怎样 |
|---|---|---|
| `queue_v3.csv`（尺寸=当前官方） | 让 `inspect_content` 的大小检查能过 | 复用上来仍判 `content_anomaly`，复用空转 |
| `--reuse-parts`（下载器新开关） | 命中 `.part` 就**不发 ZIP 请求** | 尺寸对了但要重下 2,131 份 |

---

## 1. 纳管前置检查（必须全部通过）

```bash
# ① 唯一写入者已退出：进程不在、日志不再增长
py -3.10 tools/archive_queue_ds.py status --version v2

# ② 无遗留停止哨兵（有就先清，否则下次启动被误停）
ls data/archive_raw/.archive_download.stop 2>/dev/null && \
  py -3.10 tools/archive_queue_ds.py clear-stop

# ③ 生产队列未被污染（应为 2026-10-06 17:36 的原始时间戳）
ls -la reports/archive_download_ds_full/queue_v2.csv
```

**硬约束**：`queue_v2.csv` 全程只读。上面第 ③ 步是核对，不是修改。

---

## 2. 执行纳管（两步，可分开跑，顺序不可颠倒）

### 第 1 步：复用转正（省掉 2,131 次下载）

```bash
py -3.10 tools/archive_queue_ds.py run --version v3 --reuse-parts \
    --chunk 5000 --min-free-gib 20
```

**会发生什么**：

- 队列换成 `queue_v3.csv`（其余 99.8% 行与 v2 **逐字节相同**，只有 2,131 行尺寸变了）；
- 台账仍用 `_state.json`（**不新建**，所以已完成的 91 万项不会被当成未完成）；
- 这 2,131 项命中 `.part` → **不发 ZIP 请求**，只发 CHECKSUM 请求做校验；
- 校验通过 → 字节从 `.part` **移动**到正式路径，记 `success`；
- 校验不过 → 仍记 `content_anomaly`，字节留在 `.part`（**不转正**）。

跑完后这 2,131 项应全部进入 `success`，`.part` 数从 2,131 降到 **0**。

### 第 2 步：回归常态（复用已无对象，可关开关）

```bash
py -3.10 tools/archive_queue_ds.py run --version v3 --chunk 5000 --min-free-gib 20
```

`.part` 已被第 1 步消费完，继续带 `--reuse-parts` 也无害、但没必要。
第 2 步之后 v3 队列按正常流程推进剩余未完成项。

---

## 3. 验收口径（四条，逐条可查）

| # | 验收项 | 怎么查 |
|---|---|---|
| 1 | 命中 `.part` **不下载 ZIP** | `run_v3.log` 里这 2,131 项所在块的请求数应接近"项数×1"（只 CHECKSUM），而非 ×2 |
| 2 | 校验失败**不转正** | 扫正式路径，不得出现"文件在但台账 `content_anomaly`"的组合 |
| 3 | 重跑**不重复下载** | 紧接着再跑一次，该批应记 `本地完整`，且请求数为 0 |
| 4 | 原已完成项**不回退** | `completed` 只增不减（91 万 → 911,000+） |

离线证据：`tools/rehearse_v3_reuse.py` 已在沙盒里把 1–3 三条跑通并断言
（场景 1 异常不转正 / 场景 2 转正且 ZIP 请求=0 / 场景 3 重跑零请求且摘要保留）。

---

## 4. 回滚

**代码**：`backup_20261008_part_reuse/archive_download_ds.py`
（SHA256 `1adeb974…`，与改动前一致）→ 覆盖回 `tools/` 即恢复默认关闭行为。

**队列**：`queue_v2.csv` 从未被改，删掉 `queue_v3.csv` 即回到改动前。

**台账**：`_state.json` 由下载器自己维护，不做手工回滚；若第 1 步发现问题，
停在第 1 步、保留 `.part`、把现象报出来即可——`.part` 是留证设计，丢了才是真损失。

---

## 5. 明确不做的事

- ❌ 不手工搬 `.part` 到正式路径（那等于手工伪造完成）
- ❌ 不手工往台账写 `verified`（校验值必须由下载器从来源现取现比）
- ❌ 不放宽 `inspect_content` 的大小/结构检查
- ❌ 不原地改 `queue_v2.csv`（v3 是独立新文件）
- ❌ 不批量重下 2,131 项（复用的意义就在这里）
