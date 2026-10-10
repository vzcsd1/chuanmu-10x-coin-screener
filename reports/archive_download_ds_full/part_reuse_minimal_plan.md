# `.part` 复用最小接入方案

日期：2026-10-08
状态：**❌ 已作废 —— 本文的推荐路径已被主代理否决，请勿执行**

> **作废说明（2026-10-08 18:30）**
>
> 本文推荐「方案 C → 方案 A」分两步（先修清单重下、以后再加复用）。
> 主代理已明确**不采用**这条路径，改为**一次纳管**：v3 修正队列 +
> `--reuse-parts` 同时启用，不重下那 2,131 份。
>
> - 现行方案：`part_reuse_delivery.md`（本批交付）
> - 执行步骤：`v3_reuse_runbook.md`
>
> 下文保留作**设计过程记录**。其中"现有工具零处读取 `.part`"的能力盘点仍然准确，
> 方案 A 的伪代码方向也正确（已按此实现），但**"推荐 C → A"的结论已作废**。
> 另：下文说"不建议直接用方案 A 而不修清单"是对的——所以现在是 **v3 + A 一起上**，
> 而不是先跑 C 再跑 A。

## 背景

2,131 项 `content_anomaly` 的原始字节已保存在 `.part` 文件中（内容完好，
见 `anomaly_full_audit.csv` 与 `anomaly_2131_diagnosis.v2_corrected.md`）。
现在的问题是：**现有下载器不会复用这些 `.part`**，下次运行会重新走一遍
"下载 → 校验 → 因清单字节不符再标异常"的循环，白费流量。

## 现有工具能力盘点（事实）

| 函数 | 读哪个路径 | 是否认 `.part` |
|---|---|---|
| `classify_entry` | `out_dir / entry.relpath()` | ❌ 只看正式路径 |
| `build_done_set` | 同上 | ❌ |
| `run_verify` | `out_dir / e.relpath()` | ❌ |
| `run_fetch` 的 recheck 分支 | `pending_path_for()` → **`.pending`** | ❌ 不认 `.part` |
| `.part` 写入处（S_ANOMALY） | `part.with_name(name + ".part")` | — 只写不读 |

**结论：现有工具链没有任何一处会读取 `.part`。**
`.part` 是「内容异常时保留原始字节待查」的留证设计，不是「待复核队列」。

另注意两个后缀语义不同，不可混用：

| 后缀 | 产生条件 | 工具是否处理 |
|---|---|---|
| `.pending` | 校验值暂时取不到（`CHECK_FETCH_FAILED`） | ✅ `recheck` 分支会复用 |
| `.part` | 内容异常（`CHECK_MISMATCH` / 结构不符） | ❌ 无任何处理 |

## 为什么"手动搬文件"不可行

把 `.part` 改名成正式 `.zip` 再改台账，等于**绕过校验**——正是任务书禁止的
"手动搬文件冒充完成"。这三个原因为什么必须由下载器自己完成：

1. **校验入口唯一**：只有 `fetch_one` 内部会正确处理 `CHECK_MISMATCH` 后
   的重新判定，外部改名不会更新 `checksum_status`
2. **台账一致性**：`classify_entry` 要求「文件在 + 台账结论明确 + 大小相符」，
   手工改一半会留下三态不一致
3. **并发安全**：运行中唯一写入者持有 `DownloadLock`，外部写文件会破坏一致性

## 最小接入方案（三选一）

### 方案 A：新增 `--reuse-parts` 开关（推荐）

**改哪里**：`tools/archive_download_ds.py` 的 `fetch_one()` 开头。

**怎么改**（约 15 行）：在 `fetch_one` 向网络发请求**之前**，先探测
`out_dir / entry.relpath()` 加 `.part` 是否存在：

```python
# 伪代码，位置：fetch_one() 进入重试循环之前
part = (self.opts.out_dir / entry.relpath()).with_name(
    (self.opts.out_dir / entry.relpath()).name + ".part")
if self.opts.reuse_parts and part.exists():
    content = part.read_bytes()
    # 走与"刚下载完成"完全相同的后续流程：
    #   _checksum() 取来源校验值 → inspect_content() 结构检查
    #   → 通过则 _write() 提升为正式文件 + 删除 .part
    #   → 不通过则维持 S_ANOMALY（不重复下载）
    ...（复用既有下游代码，不新增判定分支）
```

**关键性质**：
- 复用**不绕过任何校验**——校验步骤一步不少，只是把"下载"换成"读本地"
- 校验通过才转正；不通过仍然是异常（**不会把搁置项算完成**）
- 新增 `--reuse-parts` 参数，默认关闭 → 不影响现有行为
- 不重下：命中 `.part` 时不发下载请求

**代价**：改 1 个文件、约 15 行、加 1 个 CLI 参数；需补 3 项测试
（命中复用 / 未命中回退下载 / 复用后校验不过仍异常）。

**依赖前置**：需要先解决清单 `remote_size_bytes` 问题——否则复用后
`inspect_content` 仍会因"与清单不符"判异常（见方案 C）。

### 方案 B：修正清单后走正常下载（最保守）

**改哪里**：不改代码。

**怎么做**：
1. 生成修正清单 `queue_v2_fixed.csv`（已产出于本轮任务）
2. 唯一写入者退出后，用修正清单**新建一个 v3 队列**
3. 正常 `run` → 下载器会重新下载这 2,131 项并正常完成

**代价**：要重下 2,131 份（约 24 MB，按当前速率约 3 分钟），但流程零风险、
零代码改动。**`.part` 不参与**，作为留证保留或清理。

### 方案 C：仅修清单，不动 `.part`（最小动作）

如果只是想让这 2,131 项**别再被标异常**：

- 用 `anomaly_corrected_manifest_v1.csv` 替换清单对应行的 `remote_size_bytes`
- 下次运行，下载器重新下载这 2,131 项，`inspect_content` 就会通过
- `.part` 保持原样（留证）

**代价**：要重下（同方案 B）。

## 推荐

**方案 C → 方案 A**，分两步：

- **立即**：用修正清单消除误报（零风险、零代码）
- **后续**（可选）：加 `--reuse-parts` 让 `.part` 具备复用能力，
  避免未来同类问题重复消耗流量

**不建议**：直接用方案 A 而不修清单——那样复用上来的字节仍会被判异常，
`--reuse-parts` 空转。

## 执行前置条件（硬约束）

1. **唯一写入者必须安全退出**（当前 `run` 进程结束、锁释放）
2. 修改队列/台账前先停下载，避免并发写
3. 修正清单以**新版本号**产出，不原地改 `queue_v2.csv`
4. 全程不手动搬 `.part`、不手工改台账

## 附：本轮已产出（未实施，仅备料）

- `anomaly_full_audit.csv` —— 2,131 项逐项审计
- `anomaly_corrected_manifest_v1.csv` —— 修正后的清单子集（独立文件）
- `anomaly_2131_diagnosis.v2_corrected.md` —— 更正后的诊断报告
