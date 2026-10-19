import zipfile, os, statistics, collections, sys, io, csv
ROOT = 'data/archive_raw/spot/klines'
STABLE = {'USDT','USDC','FDUSD','TUSD','DAI','BUSD','USDP','EUR','GBP','AEUR','XUSD','USDE','SUSDE','FDUSD','USD1','RLUSD','USDS','BGB','USDTB','XAUT'}
# 找共同最新月份
months = collections.Counter()
for s in os.listdir(ROOT):
    d = os.path.join(ROOT, s, '5m')
    if not os.path.isdir(d): continue
    for f in os.listdir(d):
        months['-'.join(f.replace('.zip','').split('-')[-2:])] += 1
top = months.most_common(6)
print('覆盖率最高的月份:', top)
top = sorted(top, key=lambda x: x[0])
target = top[-1][0]
print('使用月份:', target)
rows = []
for s in os.listdir(ROOT):
    fp = os.path.join(ROOT, s, '5m', f'{s}-5m-{target}.zip')
    if not os.path.isfile(fp): continue
    base = s[:-4] if s.endswith('USDT') else None
    if base is None: continue
    if base.upper() in STABLE: continue
    try:
        z = zipfile.ZipFile(fp)
        raw = z.read(z.namelist()[0]).decode()
    except Exception as e:
        continue
    qv = 0.0; bars = 0; last_ts = 0
    for line in raw.strip().split('\n'):
        p = line.split(',')
        if len(p) < 8: continue
        try:
            qv += float(p[7]); bars += 1; last_ts = max(last_ts, int(p[0]))
        except: pass
    if bars: rows.append((s, qv, bars, last_ts))
print('该月有数据的USDT交易对: %d' % len(rows))
# 日均成交额
day_qv = [(s, qv/30.0, bars, last_ts) for s, qv, bars, last_ts in rows if qv > 0]
print('有效(有成交): %d' % len(day_qv))
qs = sorted([q for _, q, _, _ in day_qv])
print('日均成交额分位: p10=%.2fM p25=%.2fM p50=%.2fM p75=%.2fM p90=%.2fM' % (
    qs[len(qs)//10]/1e6, qs[len(qs)//4]/1e6, statistics.median(qs)/1e6,
    qs[3*len(qs)//4]/1e6, qs[9*len(qs)//10]/1e6))
# 复现生产闸门
MIN, MAX = 0.5e6, 50e6
passed = [(s, q, b, t) for s, q, b, t in day_qv if MIN <= q <= MAX]
print()
print('通过成交额闸门[0.5M,50M]: %d 个' % len(passed))
passed.sort(key=lambda x: -x[1])
print('生产扫描上限 max_symbols=400')
if len(passed) > 400:
    cut = passed[400]
    print('>>> 实际发生截断！第400名日均成交额 = %.2fM' % (cut[1]/1e6))
    print('>>> 被截断的 %d 个币, 日均成交额 <= %.2fM' % (len(passed)-400, cut[1]/1e6))
    dropped = passed[400:]
    print('被截断币的成交额区间: %.2fM ~ %.2fM' % (min(d[1] for d in dropped)/1e6, max(d[1] for d in dropped)/1e6))
else:
    print('>>> 未发生截断（通过闸门的币 %d <= 400）' % len(passed))
# 闸门本身排除了多少
below = sum(1 for _,q,_,_ in day_qv if q < MIN)
above = sum(1 for _,q,_,_ in day_qv if q > MAX)
print()
print('闸门排除: <0.5M %d 个 (%.0f%%)   >50M %d 个 (%.0f%%)' % (
    below, 100*below/len(day_qv), above, 100*above/len(day_qv)))
import json
json.dump([(s,q) for s,q,_,_ in day_qv], open('_sb_pool_snapshot.json','w'))
print('\n已存 _sb_pool_snapshot.json')
