# R2 最终代码针对性验收（2026-10-07）

结论：仍需两类小范围返修；不恢复功能扩展。不是因为测试数量不足，而是最终代码可复现违反本轮契约的实际行为。未请求真实行情、未修改生产代码或归档进程。

## ds 阻塞：跨实例新暂停仍会被旧成功清除

位置：binance_box_strategy.py 的 RequestGuard.run、RateLimitState.clear_if_expired/record_pause/mark_probe/save。预约路径有锁，但暂停修改与成功清理未统一在锁内重新读取、合并及保存。

复现：A 读到已过期暂停开始恢复请求；请求期间 B 从同一状态文件重新加载并登记未来一小时的新暂停；A 成功回到 clear_if_expired，用自己仍过期的旧副本删除暂停、整表保存。实测 NEW_PAUSE_PRESERVED=False。同一个状态对象上的测试不能代表两个独立实例。

返修范围：所有修改同一状态文件的路径遵守统一的跨进程锁、最新快照和条件更新，不能只锁额度预约；成功只清理允许清理的旧暂停，不能覆盖新暂停或额度。避免重复持锁死锁。补上述真实 RequestGuard 路径的两个实例测试，必要时子进程测试；原预约与额度测试保持通过。不要为此重写整个系统。

## glm 阻塞：状态 ok 被当成字段完整，时间取最大仍掩盖缺口

位置：binance_box_strategy.py:912-921 过滤持仓零值且 25 根即 ok_oi=True；:948 合并时间用 max；:1266-1283 摘要只计 status=ok，并对每币的合并时间再取 min。

两项实际扫描函数+假接口复现：

1. OI 只有40根小时数据，LS有200根。输出 oi_chg_3d=None、deriv_status=ok、data_complete=True。三日计算所需历史未齐，不能宣称完整。
2. OI 200根的尾部30根为合法0值，LS正常200根。OI尾零被过滤后用第169号时点继续算分；摘要 data_complete=True，as_of_min=1700716400000（最新LS），而实际用的OI时间为1700608400000，相差30小时。跨币取最小并没有解决同币跨字段取最大的问题。

返修范围：先明确每项评分输入是否可计算及它自己的观测时间，整轮完整性据此判定，不能只看 status=ok。合法0值、没有历史、接口异常分别处理；暂不改变研究公式或任设时效阈值，遇到解释不清的情况保留原始事实并标不完整。旧数据不能被新字段时间盖住。补这两项测试；正常完整样本集合与评分保持不变（已修回退误计分除外）。

## 最小复现

项目根目录执行以下 Python，使用临时目录自动清理、假接口、无真实网络请求。可保存于代理自己的验收目录复跑，不写真实 results/。

```python
import tempfile
from pathlib import Path
import binance_box_strategy as b
import tests.test_live_futures_acceptance_glm as g

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / 'state.json'
    a = b.RateLimitState.load(p)
    key = 'futures|direct'
    now = b._now_ms()
    a.domains[key] = {'banned_until_ms': now-1,
                      'paused_since_ms': now-10000,
                      'probe_at_ms': 0, 'reason': 'old'}
    a.save()
    guard = b.RequestGuard(a, b.Config(proxy=None, rate_limit_state=str(p)))
    def late_success():
        other = b.RateLimitState.load(p)
        other.record_pause(key, now+3600000, 'new pause', now=now)
        return 'ok'
    guard.run(b.FUTURES_DOMAIN, late_success)
    print('NEW_PAUSE_PRESERVED', b.RateLimitState.load(p).until(key) == now+3600000)

for kind in ('short', 'old'):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        stats = {}
        oi = g.oi_hist(n=40) if kind == 'short' else g.oi_hist(growth=0.005, tail_zeros=30)
        fut = g.FakeFutures(oi=oi, ls=g.ls_hist())
        rows = g.run_scan(g.FakeSpot({'DOGE/USDT': g.rising_klines()}),
                          fut, g.make_cfg(tmp), round_stats=stats)
        print(kind, stats['data_complete'], rows[0].get('oi_chg_3d'),
              rows[0]['deriv_status'], stats['as_of_min'])
```

## 派单停止范围

ds 与 glm 沿用 tasks/live_futures_r2.md 的共享文件顺序交接，分别只处理上面一类阻塞。最终主代理复跑反例；通过后进入正常节奏的真实查询验收，不再因为键缺席/null、统一格式、美化或更全面工程继续派单。真实验证仍未执行，不宣布原封禁根因或持续可用已证明。

本轮测试使用最终核心文件。报告时间仅代表这一轮本地代码，不把未复测部分称为全量验收通过。
