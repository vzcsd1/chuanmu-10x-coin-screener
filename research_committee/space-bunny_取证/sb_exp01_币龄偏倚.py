"""决定性对照：研究样本 vs 全库，看'新币是否被系统性漏掉'。
只读归档库与现有 csv，不改生产。"""
import os,csv,statistics,collections,datetime,zipfile,json
ROOT='data/archive_raw/spot/klines'
_cache={}
def first_ts(sym):
    if sym in _cache: return _cache[sym]
    d=os.path.join(ROOT,sym,'5m')
    if not os.path.isdir(d): _cache[sym]=None; return None
    fs=sorted(os.listdir(d))
    if not fs: _cache[sym]=None; return None
    try:
        z=zipfile.ZipFile(os.path.join(d,fs[0]))
        line=z.read(z.namelist()[0]).decode().split('\n',1)[0]
        ts=int(line.split(',')[0])
        v=ts//1000 if ts<10**14 else ts//1000000
    except Exception:
        v=None
    _cache[sym]=v
    return v
def D(ts): return datetime.datetime(1970,1,1)+datetime.timedelta(seconds=ts)

# 全库 USDT 币的币龄（以归档最后月份为基准日）
months=collections.Counter()
info={}
for s in os.listdir(ROOT):
    d=os.path.join(ROOT,s,'5m')
    if os.path.isdir(d):
        ms=sorted(x for x in ('-'.join(f.replace('.zip','').split('-')[-2:]) for f in os.listdir(d))
                 if len(x)==7 and x[4]=='-' and x[:4].isdigit() and x[5:].isdigit())
        if not ms: continue
        info[s]=ms
        months[ms[-1]]+=1
latest=sorted(months)[-1]
base_day=datetime.datetime.strptime(latest+'-28','%Y-%m-%d')
print('基准日(归档最后月 %s 月末): %s'%(latest,base_day.date()))

U=[s for s in info if s.endswith('USDT')]
pop=[]
for s in U:
    t=first_ts(s)
    if t is None: continue
    pop.append(((base_day-D(t)).days,s))
pop.sort()
n=len(pop)
print('全库USDT可算币龄: %d 个'%n)
print()
print('=== 全库现状：币龄分布（每个分位）===')
for lo,hi,nm in [(0,90,'<3月'),(91,180,'3-6月'),(181,365,'6-12月'),(366,730,'1-2年'),(731,1460,'2-4年'),(1461,99999,'>4年')]:
    c=sum(1 for a,_ in pop if lo<=a<=hi)
    print('  %-7s %4d (%4.1f%%)  %s'%(nm,c,100*c/n,'#'*int(50*c/n)))

rows=list(csv.DictReader(open('data/backtest_scores.csv',encoding='utf-8')))
K='\ufeffsymbol'
print()
print('=== 关键对照：历史研究样本 vs 全库现状 ===')
for tier in ['2-3x','3-5x','5-10x','10x+']:
    ages=[]
    for r in rows:
        if r['label']!='1' or r['tier']!=tier: continue
        t=first_ts(r[K])
        if t is None: continue
        try: ev=datetime.datetime.strptime(r['date'],'%Y-%m-%d')
        except Exception: continue
        ages.append((ev-D(t)).days)
    if not ages: continue
    young=sum(1 for a in ages if a<=730)
    print('  %-7s n=%3d 中位=%4d天  <=2年占比=%.0f%%'%(tier,len(ages),statistics.median(ages),100*young/len(ages)))
allages=[]
for r in rows:
    if r['label']!='1': continue
    t=first_ts(r[K])
    if t is None: continue
    try: ev=datetime.datetime.strptime(r['date'],'%Y-%m-%d')
    except Exception: continue
    allages.append((ev-D(t)).days)
y=sum(1 for a in allages if a<=730)
print('  %-7s n=%3d 中位=%4d天  <=2年占比=%.0f%%'%('全部爆拉',len(allages),statistics.median(allages),100*y/len(allages)))
py=sum(1 for a,_ in pop if a<=730)
print('  %-7s n=%4d 中位=%4d天  <=2年占比=%.0f%%'%('全库现状',n,statistics.median([a for a,_ in pop]),100*py/n))

print()
print('=== 十倍币明细：爆拉时的币龄 ===')
d10=[]
for r in rows:
    if r['tier']!='10x+': continue
    t=first_ts(r[K])
    if t is None: continue
    ev=datetime.datetime.strptime(r['date'],'%Y-%m-%d')
    d10.append(((ev-D(t)).days, r[K], r['date'], float(r['gain'])))
d10.sort()
for a,s,d,g in d10:
    print('  %-14s %s  币龄%5d天(%4.1f年)  涨幅%.1fx'%(s,d,a,a/365,g))
json.dump({'pop_n':n,'pop_young_pct':100*py/n,
           'samples':{'all_n':len(allages),'all_young_pct':100*y/len(allages)}},
          open('_sb_bias.json','w'),ensure_ascii=False,indent=1)
