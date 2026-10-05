"""Binance public-data box/selection monitor.

This version never places orders and needs no API key. It uses public Binance
spot/futures endpoints and optionally CoinGecko's public market-cap endpoint.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt
import pandas as pd
import requests


# Pegged assets quoted in USDT: excluded from scanning, not from market-cap lookups.
STABLECOINS = frozenset({
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EURI", "AEUR",
    "PYUSD", "USD1", "USDE", "USDS", "XUSD",
})


# ---------------------------------------------------------------------------
# 评分权重 —— 单一真源
#
# 权重不是拍脑袋定的，来自对 841 个历史爆拉事件的实测：
#   research/08_audit_hindsight.py  给出每个条件的事前区分度
#                                  （爆拉组命中率 − 对照组命中率，单位 pp）
#   research/07_oos_validate.py     给出时间切分样本外检验（训练 <2025-01-01）
#
# 入选（区分度为正且样本外方向稳定）：
#   收盘 > EMA50       +35.3pp  样本外 +34.3/+27.5  -> 5
#   OI 1 日增 > 10%    +12.6pp  提升 1.57x          -> 4
#   OI 3 日增 > 20%    + 8.9pp  提升 1.47x          -> 3
#   大户持仓多空比 < 1  + 8.0pp  提升 1.38x          -> 3
#   已突破箱体         + 5.7pp  提升 1.33x          -> 2
#   24h 量增           + 8.9pp  样本外稳定          -> 1
#
# 移出评分、仅作展示（实测为噪音，或样本外方向翻转）：
#   箱体宽 <=12% (+0.1pp) / 量比 >=1.5 (0.0pp) / 量能三台阶 (+2.6pp)
#   4h 量增 (+2.2pp) / 箱体持续 >=72h (训练 +11.7 → 测试 −10.6) / 资金费率 >=0 (翻转)
#   多空比 3 日下降 —— 原用"全市场账户比"测得 +7.7pp，但把口径对齐实盘
#   （大户持仓比）后只剩 +1.5pp，属噪音，故移出评分（见 research/05b_patch_ls_top_chg.py）
#
# 直接删除（实测为噪音）：
#   涨幅前十 加分项 (−0.0pp，提升 0.99x)
#     注：早前报告记为 −19.3pp「比随机还差」，那是错误数字。当时在**样本内**做
#     横截面排名，而样本每天中位仅 1 行（98.3% 的天数 ≤10 行），rank<=10 恒为真、
#     条件退化。按全市场重算后为纯噪音。删除它是对的，理由是「无信息」而非「反向」。
#     修复见 research/08_audit_hindsight.py 的 market_daily_returns()/market_rank_1d()
#   48h 波动 15–25% (+1.0pp)
#
# research/09_backtest_revised.py 直接 import 本常量做反向回测，确保
# 回测口径与实盘口径永远一致；改权重只需改这一处。
# ---------------------------------------------------------------------------
WEIGHTS: dict[str, int] = {
    "c_trend": 5,       # 收盘 > EMA50（趋势）
    "oi_chg_1d": 4,     # OI 1 日增速 > 10%
    "oi_chg_3d": 3,     # OI 3 日增速 > 20%
    "ls_top": 3,        # 大户持仓多空比 < 1（空头拥挤）
    "c_breakout": 2,    # 已突破箱体上沿
    "c_v24up": 1,       # 24h 成交量高于前 24h
}
MAX_SCORE = sum(WEIGHTS.values())

# 触发阈值（与 research 脚本保持一致）
OI_1D_THRESHOLD = 0.10
OI_3D_THRESHOLD = 0.20
LS_TOP_THRESHOLD = 1.0


def _as_float(value: Any, default: float = 0.0) -> float:
    """Convert exchange fields safely; malformed or non-finite values become zero."""
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return default
    return converted if math.isfinite(converted) else default


@dataclass(frozen=True)
class Config:
    timeframe: str = "1h"
    lookback: int = 240
    box_period: int = 24
    # —— 候选池闸门 ——
    # 下限：只保证基本可交易。实测最低成交额档（中位 0.28M）爆拉率 46.2%，
    #   明显低于其余档（~60%），所以留一个下限，但不必设高。
    min_quote_volume: float = 500_000
    # 上限：成交额是市值的代理（log 相关 +0.738），成交额越大 = 市值越大 =
    #   越不可能十倍。19 个历史十倍币启动前成交额最大仅 14.87M；上限设 50M
    #   只漏 2.7% 的爆拉币、0% 的十倍币。
    max_quote_volume: float = 50_000_000
    # 市值硬闸门：即使按最保守的换手率（1%）反推，最大的十倍币启动时也只有
    #   约 1.5B 市值，2B 是安全上限。它能一次排除 BTC(1.71T)/ETH/SOL 等
    #   结构性不可能十倍的标的。CoinGecko 取不到市值时不拦（fail-open）。
    max_market_cap: float = 2_000_000_000
    max_symbols: int = 400
    volume_multiplier: float = 1.5
    ema_period: int = 50
    atr_period: int = 14
    stop_atr: float = 1.5
    reward_risk: float = 2.0
    risk_per_trade: float = 0.01
    max_quote_per_trade: float = 100.0
    poll_seconds: int = 300
    # 门槛 = 5（满分 18）。
    #   含义：至少凑够 5 分才能进候选池。**注意：不是"至少趋势成立"**——趋势=5 虽是
    #   单项最高分，但评分是各项**相加**：oi_chg_1d(4) + c_v24up(1) = 5 也能达标，
    #   无需趋势；门槛=4 时 oi_chg_1d(4) 更能单独达标。所以 5 是**当前设计选择**，
    #   不是数学下限（调低属策略决策，需重新验证）。
    #   为什么从 6 降到 5：K 线侧满分仅 8、单项最高是趋势=5，门槛=6 时"只有趋势"
    #   的币单靠 K 线永远进不了池，而 822 个爆拉币里 65.8% 没有合约数据可补分。
    #   K线=5 这一档**携带正信息**：历史爆拉率 68.0%，高于基准 58.6%
    #   （K线 0–4 各档只有 29–34%），所以放宽不是"退化成不筛选"。
    #   早前担心"会放进 BTC"的真正原因是没有候选池闸门；加上成交额/市值上限后，
    #   BTC 这类已被结构性排除，与门槛无关。
    #   6→5 实测：命中率 48.1% → 74.9%，10x+ 命中 9/19 → 15/19，
    #   且**样本外精确率不降反升**（测试集 55.9% → 58.2%）。
    #   证据见 爆拉币命中率与优化空间.md、research/12_optimize_gate.py。
    min_score: int = 5
    use_coingecko: bool = True
    timeout_ms: int = 30_000
    proxy: str | None = None
    csv_dir: str | None = None
    require_okx: bool = True
    require_futures: bool = False


def registry_proxy() -> str | None:
    """Read the Windows system proxy (WinINET) as a fallback.

    Double-clicking a launcher gives no inherited HTTP_PROXY, but Clash-style
    clients do register a system proxy here, so this keeps the scanner working
    outside a preconfigured shell.
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return None
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
    except OSError:
        return None
    if not server:
        return None
    # ProxyServer is either "host:port" or "http=host:port;https=host:port".
    if "=" in server:
        mapping = {}
        for part in server.split(";"):
            if "=" in part:
                scheme, _, address = part.partition("=")
                mapping[scheme.strip().lower()] = address.strip()
        server = mapping.get("https") or mapping.get("http") or ""
        if not server:
            return None
    if "://" not in server:
        server = f"http://{server}"
    return server


def resolve_proxy() -> str | None:
    """Pick a proxy URL from the environment, then the Windows system proxy.

    ccxt builds its own requests.Session with trust_env=False, so the usual
    HTTP_PROXY/HTTPS_PROXY variables are ignored unless we pass them in
    explicitly. BINANCE_PROXY wins so the scanner can be pointed elsewhere
    without touching the shell-wide proxy.
    """
    if (os.getenv("BINANCE_NO_PROXY") or "").strip().lower() in ("1", "true", "yes"):
        return None
    for name in ("BINANCE_PROXY", "HTTPS_PROXY", "https_proxy",
                 "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return registry_proxy()


def env_config() -> Config:
    defaults = Config()

    def number(name: str, default: Any, cast):
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            return cast(default)
        try:
            return cast(raw)
        except (TypeError, ValueError):
            logging.warning("invalid %s=%r; using %r", name, raw, default)
            return cast(default)

    return Config(
        timeframe=os.getenv("BINANCE_TIMEFRAME", "1h"),
        lookback=number("LOOKBACK", 240, int),
        box_period=number("BOX_PERIOD", 24, int),
        min_quote_volume=number("MIN_QUOTE_VOLUME", defaults.min_quote_volume, float),
        max_quote_volume=number("MAX_QUOTE_VOLUME", defaults.max_quote_volume, float),
        max_market_cap=number("MAX_MARKET_CAP", defaults.max_market_cap, float),
        max_symbols=number("MAX_SYMBOLS", defaults.max_symbols, int),
        volume_multiplier=number("VOLUME_MULTIPLIER", 1.5, float),
        ema_period=number("EMA_PERIOD", 50, int),
        atr_period=number("ATR_PERIOD", 14, int),
        stop_atr=number("STOP_ATR", 1.5, float),
        reward_risk=number("REWARD_RISK", 2.0, float),
        risk_per_trade=number("RISK_PER_TRADE", 0.01, float),
        max_quote_per_trade=number("MAX_QUOTE_PER_TRADE", 100, float),
        poll_seconds=number("POLL_SECONDS", 300, int),
        min_score=number("MIN_SCORE", defaults.min_score, int),
        use_coingecko=os.getenv("USE_COINGECKO", "true").lower() == "true",
        timeout_ms=number("REQUEST_TIMEOUT_MS", 30_000, int),
        proxy=resolve_proxy(),
        csv_dir=os.getenv("CSV_DIR") or str(Path(__file__).resolve().parent / "results"),
        require_okx=os.getenv("REQUIRE_OKX", "true").lower() == "true",
        require_futures=os.getenv("REQUIRE_FUTURES", "false").lower() == "true",
    )


def requests_proxies(cfg: Config) -> dict[str, str] | None:
    if not cfg.proxy:
        return None
    return {"http": cfg.proxy, "https": cfg.proxy}


def apply_proxy(exchange: ccxt.binance, cfg: Config) -> ccxt.binance:
    """Route ccxt through the configured proxy.

    ccxt rejects setting more than one proxy attribute at a time, so only the
    field matching the URL scheme is assigned.
    """
    if not cfg.proxy:
        return exchange
    if cfg.proxy.lower().startswith("socks"):
        exchange.socksProxy = cfg.proxy
    else:
        exchange.httpsProxy = cfg.proxy
    return exchange


def build_exchange(cfg: Config, default_type: str) -> ccxt.binance:
    # Public endpoints only: no credentials are read or required.
    exchange = ccxt.binance({
        "enableRateLimit": True,
        "timeout": cfg.timeout_ms,
        "options": {"defaultType": default_type},
    })
    return apply_proxy(exchange, cfg)


def connect_exchanges(cfg: Config, attempts: int = 3) -> tuple[ccxt.binance, ccxt.binance, Config]:
    """Load spot/futures markets with retries and a direct-connect fallback."""
    last_error: Exception | None = None
    configs = [cfg]
    if cfg.proxy:
        from dataclasses import replace
        configs.append(replace(cfg, proxy=None))
    for candidate in configs:
        mode = f"代理 {candidate.proxy}" if candidate.proxy else "直连"
        for attempt in range(1, attempts + 1):
            try:
                print(f"连接币安（{mode}，第 {attempt}/{attempts} 次）...")
                spot = exchange_from_env(candidate)
                futures = futures_exchange(candidate)
                spot.load_markets()
                futures.load_markets()
                return spot, futures, candidate
            except Exception as exc:
                last_error = exc
                logging.warning("Binance connection failed via %s (%d/%d): %s",
                                mode, attempt, attempts, exc)
                if attempt < attempts:
                    time.sleep(2)
    assert last_error is not None
    raise last_error


def exchange_from_env(cfg: Config) -> ccxt.binance:
    return build_exchange(cfg, "spot")


def futures_exchange(cfg: Config) -> ccxt.binance:
    return build_exchange(cfg, "future")


def okx_exchange(cfg: Config) -> ccxt.okx:
    exchange = ccxt.okx({"enableRateLimit": True, "timeout": cfg.timeout_ms})
    if cfg.proxy:
        if cfg.proxy.lower().startswith("socks"):
            exchange.socksProxy = cfg.proxy
        else:
            exchange.httpsProxy = cfg.proxy
    return exchange


def indicators(ohlcv: list[list[float]], cfg: Config) -> pd.DataFrame:
    if not ohlcv:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume",
                                     "atr", "ema", "box_high", "box_low", "avg_volume"])
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    for column in ["ts", "open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["ts", "open", "high", "low", "close", "volume"]).reset_index(drop=True)
    prev_close = df["close"].shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = true_range.rolling(cfg.atr_period).mean()
    df["ema"] = df["close"].ewm(span=cfg.ema_period, adjust=False).mean()
    # Exclude the current candle: a signal is evaluated only after it closes.
    df["box_high"] = df["high"].shift(1).rolling(cfg.box_period).max()
    df["box_low"] = df["low"].shift(1).rolling(cfg.box_period).min()
    df["avg_volume"] = df["volume"].shift(1).rolling(cfg.box_period).mean()
    return df


def signal(symbol: str, df: pd.DataFrame, cfg: Config) -> dict[str, Any] | None:
    if len(df) < max(cfg.box_period, cfg.ema_period, cfg.atr_period) + 3:
        return None
    row = df.iloc[-2]  # last fully closed candle
    required = ["box_high", "box_low", "avg_volume", "atr", "ema"]
    if any(pd.isna(row[x]) for x in required):
        return None
    breakout = row.close > row.box_high
    volume_ok = row.volume >= row.avg_volume * cfg.volume_multiplier
    trend_ok = row.close > row.ema
    if not (breakout and volume_ok and trend_ok):
        return None
    stop = max(row.close - cfg.stop_atr * row.atr, row.box_high * 0.995)
    risk_per_unit = row.close - stop
    if risk_per_unit <= 0:
        return None
    return {
        "symbol": symbol,
        "time": datetime.fromtimestamp(row.ts / 1000, tz=timezone.utc).isoformat(),
        "entry": float(row.close),
        "box_high": float(row.box_high),
        "box_low": float(row.box_low),
        "atr": float(row.atr),
        "stop": float(stop),
        "take_profit": float(row.close + cfg.reward_risk * risk_per_unit),
        "volume_ratio": float(row.volume / row.avg_volume),
    }


def box_score(df: pd.DataFrame, cfg: Config) -> tuple[int, dict[str, float]]:
    """K 线侧评分（权重见模块级 WEIGHTS）。

    只有实测有区分度且样本外方向稳定的条件参与计分；其余条件仍写入
    details 供展示，但不再计分。依据见 WEIGHTS 上方的实测说明。
    """
    if len(df) < 2:
        return 0, {}
    row = df.iloc[-2]
    required = ["box_high", "box_low", "avg_volume", "atr", "ema", "close", "volume"]
    if any(column not in df or pd.isna(row[column]) for column in required):
        return 0, {}
    score = 0
    details: dict[str, float] = {}
    width = (row.box_high - row.box_low) / row.close if row.close else 999
    # 箱体宽 <=12% 实测区分度 +0.1pp（样本外 −17.8pp）：展示，不计分
    details["box_width_pct"] = float(width * 100)
    if row.close > row.box_high:
        score += WEIGHTS["c_breakout"]
        details["c_breakout"] = 1.0
    if row.avg_volume:
        ratio = float(row.volume / row.avg_volume)
        # 量比 >=1.5 实测区分度 0.0pp：展示，不计分
        details["volume_ratio"] = ratio
        if ratio >= cfg.volume_multiplier:
            details["vol_surge"] = 1.0
    if row.close > row.ema:
        score += WEIGHTS["c_trend"]
        details["c_trend"] = 1.0
    closed_volume = df["volume"].iloc[:-1]
    if len(closed_volume) >= 12:
        v0, v1 = float(closed_volume.iloc[-4:].sum()), float(closed_volume.iloc[-8:-4].sum())
        # 4h 量增 / 量能三台阶实测区分度 +2.2pp / +2.6pp：展示，不计分
        details["volume_4h_growth"] = v0 / v1 if v1 else 0.0
    if len(closed_volume) >= 48:
        v24 = float(closed_volume.iloc[-24:].sum())
        prev24 = float(closed_volume.iloc[-48:-24].sum())
        details["volume_24h_growth"] = v24 / prev24 if prev24 else 0.0
        if v24 > prev24:
            score += WEIGHTS["c_v24up"]
            details["c_v24up"] = 1.0
    return score, details


def deriv_score(oi_chg_1d: float | None = None, oi_chg_3d: float | None = None,
                ls_top: float | None = None,
                ls_chg_3d: float | None = None) -> tuple[int, dict[str, float]]:
    """合约侧评分：OI 增速 + 大户持仓多空比（权重见模块级 WEIGHTS）。

    全部输入都是评估时点已经实现的历史量，不含任何未来数据。
    这是项目原先完全缺失、而实测提升倍数最高的一个维度。
    """
    score = 0
    details: dict[str, float] = {}

    def finite(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    if finite(oi_chg_1d):
        details["oi_chg_1d"] = float(oi_chg_1d)
        if oi_chg_1d >= OI_1D_THRESHOLD:
            score += WEIGHTS["oi_chg_1d"]
            details["hit_oi_1d"] = 1.0
    if finite(oi_chg_3d):
        details["oi_chg_3d"] = float(oi_chg_3d)
        if oi_chg_3d >= OI_3D_THRESHOLD:
            score += WEIGHTS["oi_chg_3d"]
            details["hit_oi_3d"] = 1.0
    if finite(ls_top):
        details["ls_top"] = float(ls_top)
        if ls_top < LS_TOP_THRESHOLD:
            score += WEIGHTS["ls_top"]
            details["hit_ls_top"] = 1.0
    if finite(ls_chg_3d):
        # 实测（大户口径）区分度 +1.5pp，属噪音：仅展示，不计分
        details["ls_chg_3d"] = float(ls_chg_3d)
    return score, details


DERIV_PERIOD = "1h"
DERIV_LOOKBACK = 200  # 1h × 200 ≈ 8.3 天，覆盖 3 日增速所需窗口


def fetch_deriv_context(futures: ccxt.binance, symbol: str, cfg: Config) -> dict[str, Any]:
    """拉取历史 OI 与大户持仓多空比，算出评估时点已知的增速。

    数据源是币安公开的 /futures/data/* 接口，无需 API Key；该组接口只保留
    最近 30 天，足够计算 1 日与 3 日变化。

    口径与 research/05_deriv_features.py 对齐：
      oi_chg_1d / oi_chg_3d  基于 sumOpenInterestValue，与 metrics 的
                             sum_open_interest_value 同源
      ls_top                 基于 topLongShortPositionRatio（大户持仓多空比），
                             对应 metrics 的 sum_toptrader_long_short_ratio；
                             该接口不可用时回退到 ccxt 的全市场账户比，并在
                             ls_source 标注实际口径，避免口径混淆
    """
    out: dict[str, Any] = {"oi_chg_1d": None, "oi_chg_3d": None,
                           "ls_top": None, "ls_chg_3d": None, "ls_source": None}
    market_id = futures.market(symbol)["id"]

    try:
        raw = futures.fapiDataGetOpenInterestHist(
            {"symbol": market_id, "period": DERIV_PERIOD, "limit": DERIV_LOOKBACK})
        series = [(_as_float(item.get("sumOpenInterestValue"), float("nan")),
                   int(item.get("timestamp") or 0)) for item in raw]
        series = [(v, t) for v, t in series if math.isfinite(v) and v > 0 and t]
        if len(series) >= 25:
            current_value, current_ts = series[-1]
            for back_hours, key in ((24, "oi_chg_1d"), (72, "oi_chg_3d")):
                cutoff = current_ts - back_hours * 3_600_000
                past = [v for v, t in series if t <= cutoff]
                if past and past[-1] > 0:
                    out[key] = current_value / past[-1] - 1.0
    except Exception as exc:  # noqa: BLE001
        logging.debug("OI history unavailable for %s: %s", symbol, exc)

    try:
        raw = futures.fapiDataGetTopLongShortPositionRatio(
            {"symbol": market_id, "period": DERIV_PERIOD, "limit": DERIV_LOOKBACK})
        series = []
        for item in raw:
            ratio = _as_float(item.get("longShortRatio"), float("nan"))
            if not math.isfinite(ratio):
                long_account = _as_float(item.get("longAccount"), float("nan"))
                short_account = _as_float(item.get("shortAccount"), float("nan"))
                if math.isfinite(long_account) and math.isfinite(short_account) and short_account > 0:
                    ratio = long_account / short_account
            if math.isfinite(ratio):
                series.append((ratio, int(item.get("timestamp") or 0)))
        if series:
            out["ls_top"] = series[-1][0]
            out["ls_source"] = "topLongShortPositionRatio"
            cutoff = series[-1][1] - 72 * 3_600_000
            past = [v for v, t in series if t and t <= cutoff]
            if past:
                out["ls_chg_3d"] = series[-1][0] - past[-1]
    except Exception as exc:  # noqa: BLE001
        logging.debug("top position ratio unavailable for %s: %s", symbol, exc)
        try:
            raw = futures.fetch_long_short_ratio_history(symbol, DERIV_PERIOD, limit=DERIV_LOOKBACK)
            series = [(float(item["longShortRatio"]), int(item.get("timestamp") or 0))
                      for item in raw if item.get("longShortRatio") is not None]
            if series:
                out["ls_top"] = series[-1][0]
                out["ls_source"] = "globalLongShortAccountRatio(回退口径)"
                cutoff = series[-1][1] - 72 * 3_600_000
                past = [v for v, t in series if t and t <= cutoff]
                if past:
                    out["ls_chg_3d"] = series[-1][0] - past[-1]
        except Exception as exc2:  # noqa: BLE001
            logging.debug("long/short ratio unavailable for %s: %s", symbol, exc2)

    return out


def coingecko_caps(cfg: Config) -> dict[str, float]:
    """Best-effort public market caps keyed by uppercase ticker symbol."""
    caps: dict[str, float] = {}
    proxies = requests_proxies(cfg)
    for page in range(1, 5):
        response = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250,
                    "page": page, "sparkline": "false"},
            timeout=cfg.timeout_ms / 1000,
            proxies=proxies,
        )
        response.raise_for_status()
        for coin in response.json():
            symbol = str(coin.get("symbol", "")).upper()
            cap = coin.get("market_cap")
            if symbol and cap:
                # Symbol collisions exist; retaining the largest cap is conservative.
                caps[symbol] = max(caps.get(symbol, 0), float(cap))
    return caps


def public_selection(exchange: ccxt.binance, futures: ccxt.binance, cfg: Config,
                     okx: ccxt.okx | None = None) -> list[dict[str, Any]]:
    """Rank Binance symbols with the programmable parts of the Chuanmu notes."""
    tickers = exchange.fetch_tickers()
    futures_tickers = futures.fetch_tickers()
    okx_markets: dict[str, Any] = {}
    okx_tickers: dict[str, Any] = {}
    if okx is not None:
        try:
            okx_markets = okx.load_markets()
            okx_tickers = okx.fetch_tickers()
        except Exception as exc:
            logging.warning("OKX data unavailable: %s", exc)
    future_by_base = {
        market["base"]: symbol for symbol, market in futures.markets.items()
        if market.get("swap") and market.get("linear") and market.get("quote") == "USDT"
        and market.get("active", True)
    }
    caps: dict[str, float] = {}
    if cfg.use_coingecko:
        try:
            caps = coingecko_caps(cfg)
        except Exception as exc:
            logging.warning("CoinGecko market caps unavailable: %s", exc)
    rows: list[dict[str, Any]] = []
    symbols = select_symbols(tickers, cfg, caps)
    logging.info("扫描范围 %d 个；成交额闸门 [%s, %s]；市值上限 %s；观察门槛 %d；%s",
                 len(symbols), human_money(cfg.min_quote_volume),
                 human_money(cfg.max_quote_volume), human_money(cfg.max_market_cap),
                 cfg.min_score,
                 "仅含合约标的" if cfg.require_futures else "包含仅现货标的")
    for index, symbol in enumerate(symbols, 1):
        if index == 1 or index % 10 == 0 or index == len(symbols):
            logging.info("扫描进度 %d/%d: %s", index, len(symbols), symbol)
        try:
            market = exchange.market(symbol)
            if market.get("active") is False or not market.get("spot", True):
                continue
            base = market["base"]
            future_symbol = future_by_base.get(base)
            if cfg.require_futures and not future_symbol:
                continue
            candles = exchange.fetch_ohlcv(symbol, cfg.timeframe, limit=cfg.lookback)
            df = indicators(candles, cfg)
            if len(df) < cfg.box_period + 3:
                continue
            score, details = box_score(df, cfg)
            kline_points = score
            trade_levels = signal(symbol, df, cfg) or {}
            ft = futures_tickers.get(future_symbol, {})
            quote_volume = _as_float(tickers[symbol].get("quoteVolume"))
            futures_volume = _as_float(ft.get("quoteVolume"))
            cap = caps.get(base)
            volume_cap_ratio = quote_volume / cap if cap else None

            # —— 辅助加分（不计入门槛）——
            # 市值 / OKX / 合约量 / OI市值比这几类条件在历史回测里缺数据、**未经验证**，
            # 若混进 score 会让实盘门槛与回测门槛口径不一致（例如 BTC 仅凭"趋势+合约量+OKX"
            # 就能凑到 7 分过门槛）。因此单独累计到 extra_score，只作展示与人工参考。
            extra_score = 0
            extra_hits: list[str] = []
            if future_symbol and futures_volume >= quote_volume:
                extra_score += 1
                extra_hits.append("合约量>=现货量")
            if cap and cap <= 50_000_000:
                extra_score += 2
                extra_hits.append("市值<=5000万")
            elif cap and cap <= 100_000_000:
                extra_score += 1
                extra_hits.append("市值<=1亿")
            if volume_cap_ratio and volume_cap_ratio >= 0.60:
                extra_score += 2
                extra_hits.append("成交量/市值>=60%")
            oi_value = None
            if future_symbol:
                try:
                    oi = futures.fetch_open_interest(future_symbol)
                    oi_value = _as_float(oi.get("openInterestValue"))
                    if not oi_value:
                        oi_value = _as_float(oi.get("openInterestAmount")) * _as_float(ft.get("last") or ft.get("close"))
                except Exception:
                    logging.debug("open interest unavailable for %s", future_symbol, exc_info=True)
            oi_cap_ratio = oi_value / cap if cap and oi_value else None
            if oi_cap_ratio and oi_cap_ratio >= 0.30:
                extra_score += 1
                extra_hits.append("OI/市值>=30%")
            if oi_cap_ratio and oi_cap_ratio >= 1.0:
                extra_score += 1
                extra_hits.append("OI>=市值")
            okx_swap = any(
                market.get("base") == base and market.get("quote") == "USDT"
                and market.get("swap") and market.get("active", True)
                for market in okx_markets.values()
            )
            if okx_swap:
                extra_score += 1
                extra_hits.append("币安+OKX合约")
            funding_rate = None
            if future_symbol:
                try:
                    funding = futures.fetch_funding_rate(future_symbol)
                    funding_rate = _as_float(funding.get("fundingRate"), default=float("nan"))
                    # 资金费率仅展示，不计分。
                except Exception:
                    logging.debug("funding unavailable for %s", future_symbol, exc_info=True)
            # 合约侧：OI 增速 + 大户持仓多空比（实测提升倍数最高的一维）
            deriv = fetch_deriv_context(futures, future_symbol, cfg) if future_symbol else {}
            deriv_points, deriv_details = deriv_score(
                deriv.get("oi_chg_1d"), deriv.get("oi_chg_3d"),
                deriv.get("ls_top"), deriv.get("ls_chg_3d"))
            score += deriv_points
            details.update(deriv_details)
            percentage = _as_float(tickers[symbol].get("percentage"), default=float("nan"))
            okx_symbol = next((s for s, m in okx_markets.items()
                               if m.get("base") == base and m.get("quote") == "USDT"
                               and m.get("swap")), None)
            okx_volume = _as_float(okx_tickers.get(okx_symbol, {}).get("quoteVolume")) if okx_symbol else 0.0
            hit_conditions = []
            if details.get("c_trend"): hit_conditions.append("收盘>EMA50")
            if details.get("hit_oi_1d"): hit_conditions.append("OI 1日增>10%")
            if details.get("hit_oi_3d"): hit_conditions.append("OI 3日增>20%")
            if details.get("hit_ls_top"): hit_conditions.append("大户持仓多空比<1")
            if details.get("c_breakout"): hit_conditions.append("突破箱体")
            if details.get("c_v24up"): hit_conditions.append("24h量增")
            if math.isfinite(percentage) and percentage > 0:
                extra_hits.append("24h上涨")
            rows.append({"symbol": symbol, "score": score, "max_score": MAX_SCORE,
                         "score_kline": kline_points,
                         "score_deriv": deriv_points if future_symbol else None,
                         "has_futures": bool(future_symbol),
                         "market_scope": "现货+合约" if future_symbol else "仅现货",
                         "extra_score": extra_score,
                         "conditions_met": len(hit_conditions),
                         "conditions": ",".join(hit_conditions) or "无",
                         "extra_conditions": ",".join(extra_hits) or "无",
                         "market_cap": cap,
                         "spot_quote_volume": quote_volume, "futures_quote_volume": futures_volume,
                         "okx_quote_volume": okx_volume, "volume_cap_ratio": volume_cap_ratio,
                         "open_interest_value": oi_value, "oi_cap_ratio": oi_cap_ratio,
                         "funding_rate": funding_rate, "percentage_24h": percentage,
                         "okx_contract": okx_swap, "ls_source": deriv.get("ls_source"),
                         **details,
                         "entry": trade_levels.get("entry"), "stop": trade_levels.get("stop"),
                         "take_profit": trade_levels.get("take_profit"),
                         "breakout": bool(df.iloc[-2].close > df.iloc[-2].box_high)})
        except Exception:
            logging.exception("failed scoring %s", symbol)
    # 原先此处会给"24h 涨幅前十"的标的 +1 分。实测该项区分度 −0.0pp、提升倍数
    # 0.99x（纯噪音，详见文件头 WEIGHTS 上方说明），本质是奖励"已经涨过的币"，已删除。
    return sorted(rows, key=lambda x: (x["score"], x["futures_quote_volume"]), reverse=True)


def select_symbols(tickers: dict[str, Any], cfg: Config,
                   caps: dict[str, float] | None = None) -> list[str]:
    """挑出扫描范围：成交额落在 [下限, 上限] 内，且市值不超过硬闸门。

    排序仍是按成交额降序，但**两端都设了闸门**，所以不再等价于"挑市值最大的
    前 N 个"——那正是 BTC 能入选候选池的原因。市值取不到时不拦（fail-open），
    避免 CoinGecko 抖动让池子忽大忽小。
    """
    caps = caps or {}
    candidates = []
    for symbol, ticker in tickers.items():
        if not symbol.endswith("/USDT") or ":" in symbol:
            continue
        base = symbol.split("/")[0]
        # Stablecoin pairs never form a tradable box; they only add noise.
        if base in STABLECOINS:
            continue
        quote_volume = _as_float(ticker.get("quoteVolume"))
        if not (cfg.min_quote_volume <= quote_volume <= cfg.max_quote_volume):
            continue
        cap = caps.get(base)
        if cap and cfg.max_market_cap and cap > cfg.max_market_cap:
            continue
        candidates.append((symbol, float(quote_volume)))
    candidates.sort(key=lambda item: item[1], reverse=True)
    return [symbol for symbol, _ in candidates[: cfg.max_symbols]]


def display_width(text: str) -> int:
    """Terminal columns used by text, counting CJK glyphs as two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int, align: str = "left") -> str:
    filler = " " * max(0, width - display_width(text))
    return filler + text if align == "right" else text + filler


def human_money(value: float | None) -> str:
    if not value:
        return "-"
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return f"{value / limit:.2f}{suffix}"
    return f"{value:.0f}"


def render_table(rows: list[dict[str, Any]]) -> str:
    """Format candidates as an aligned table for the launcher console."""
    columns = [
        ("标的", "symbol", "left", lambda r: r["symbol"].replace("/USDT", "")),
        ("市场范围", "market_scope", "left", lambda r: r.get("market_scope", "-")),
        ("评分", "score", "right", lambda r: f"{r['score']}/{r.get('max_score', MAX_SCORE)}"),
        ("辅助", "extra_score", "right", lambda r: str(r.get("extra_score", 0))),
        ("命中", "conditions_met", "right", lambda r: str(r.get("conditions_met", 0))),
        ("趋势", "c_trend", "left", lambda r: "是" if r.get("c_trend") else "-"),
        ("突破", "breakout", "left", lambda r: "是" if r.get("breakout", False) else "-"),
        ("OI1日", "oi_chg_1d", "right",
         lambda r: f"{r['oi_chg_1d'] * 100:+.0f}%" if r.get("oi_chg_1d") is not None else "-"),
        ("OI3日", "oi_chg_3d", "right",
         lambda r: f"{r['oi_chg_3d'] * 100:+.0f}%" if r.get("oi_chg_3d") is not None else "-"),
        ("大户多空", "ls_top", "right",
         lambda r: f"{r['ls_top']:.2f}" if r.get("ls_top") is not None else "-"),
        ("箱体宽", "box_width_pct", "right", lambda r: f"{r.get('box_width_pct', 0):.1f}%"),
        ("量比", "volume_ratio", "right",
         lambda r: f"{r['volume_ratio']:.2f}" if r.get("volume_ratio") else "-"),
        ("市值", "market_cap", "right", lambda r: human_money(r.get("market_cap"))),
        ("现货量", "spot_quote_volume", "right", lambda r: human_money(r.get("spot_quote_volume"))),
        ("合约量", "futures_quote_volume", "right",
         lambda r: human_money(r.get("futures_quote_volume"))),
        ("量/市值", "volume_cap_ratio", "right",
         lambda r: f"{r['volume_cap_ratio']:.2f}" if r.get("volume_cap_ratio") else "-"),
        ("OI/市值", "oi_cap_ratio", "right",
         lambda r: f"{r['oi_cap_ratio']:.2f}" if r.get("oi_cap_ratio") else "-"),
        ("入场", "entry", "right", lambda r: f"{r['entry']:.8g}" if r.get("entry") else "-"),
        ("止损", "stop", "right", lambda r: f"{r['stop']:.8g}" if r.get("stop") else "-"),
        ("止盈", "take_profit", "right",
         lambda r: f"{r['take_profit']:.8g}" if r.get("take_profit") else "-"),
    ]
    table = [[header for header, _, _, _ in columns]]
    table.extend([[fmt(row) for _, _, _, fmt in columns] for row in rows])
    widths = [max(display_width(cell[i]) for cell in table) for i in range(len(columns))]
    aligns = [align for _, _, align, _ in columns]
    lines = ["  ".join(pad(c, widths[i], aligns[i]) for i, c in enumerate(table[0])),
             "  ".join("-" * widths[i] for i in range(len(columns)))]
    lines.extend("  ".join(pad(c, widths[i], aligns[i]) for i, c in enumerate(row))
                 for row in table[1:])
    return "\n".join(lines)


def write_csv(rows: list[dict[str, Any]], out_dir: Path) -> Path:
    """Persist one scan so past picks stay reviewable."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = out_dir / f"candidates-{stamp}.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def run_once(exchange: ccxt.binance, futures: ccxt.binance, cfg: Config) -> list[dict[str, Any]]:
    okx = None
    try:
        okx = okx_exchange(cfg)
    except Exception as exc:
        logging.warning("OKX client unavailable: %s", exc)
    ranked = public_selection(exchange, futures, cfg, okx)
    chosen = [x for x in ranked if x["score"] >= cfg.min_score]
    logging.info("ranked %d candidates; score >= %d: %d", len(ranked), cfg.min_score, len(chosen))
    shown = chosen or ranked[:10]
    if chosen:
        print(f"\n=== 选出标的 {len(chosen)} 个（评分 >= {cfg.min_score}）===")
    elif ranked:
        print(f"\n=== 无标的达到评分 {cfg.min_score}，显示评分最高的前 {len(shown)} 个 ===")
    if shown:
        print(render_table(shown))
        print("\n命中条件明细:")
        for item in shown:
            print(f"{item['symbol']}: 核心[{item.get('conditions', '无')}] "
                  f"辅助[{item.get('extra_conditions', '无')}]")
            print(f"Grok提示词: 请检索 {item['symbol'].replace('/USDT','')} 最近30天在 X/Twitter 的提及度、独立用户数、情绪变化、主要提及账号和事件驱动，并区分真实讨论与机器人刷屏；给出来源链接和结论。")
    for item in chosen:
        logging.debug("CANDIDATE %s", json.dumps(item, ensure_ascii=False))
    if ranked and cfg.csv_dir:
        try:
            path = write_csv(ranked, Path(cfg.csv_dir))
            print(f"\n完整评分已保存: {path}")
        except Exception as exc:
            logging.warning("could not write CSV: %s", exc)
    return chosen


def main() -> int:
    parser = argparse.ArgumentParser(description="Binance box breakout scanner")
    parser.add_argument("--once", action="store_true", help="scan once and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = env_config()
    print("Binance 箱体突破选标的扫描器（只读公共数据，无下单路径）")
    if cfg.proxy:
        print(f"代理: {cfg.proxy}")
    else:
        print("未检测到代理。若币安被墙，请设置 BINANCE_PROXY，例如 http://127.0.0.1:7897")
    print(f"参数: 周期={cfg.timeframe} 箱体={cfg.box_period} 扫描上限={cfg.max_symbols} "
          f"门槛={cfg.min_score} 市值源={'CoinGecko' if cfg.use_coingecko else '关闭'}")
    print(f"候选池闸门: 成交额 [{human_money(cfg.min_quote_volume)}, "
          f"{human_money(cfg.max_quote_volume)}]  市值 <= {human_money(cfg.max_market_cap)}")
    print("正在拉取行情，扫描期间会输出进度。\n")
    try:
        exchange, futures, cfg = connect_exchanges(cfg)
    except Exception as exc:
        print(f"\n连接币安失败: {type(exc).__name__}: {exc}")
        log_path = Path(__file__).resolve().parent / "connection-error.log"
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        print(f"已尝试代理和直连。完整错误已保存到: {log_path}")
        print("请确认代理软件已启动，并开启系统代理或 TUN 模式。")
        return 1
    while True:
        try:
            run_once(exchange, futures, cfg)
        except KeyboardInterrupt:
            print("\n已手动停止。")
            return 0
        except Exception as exc:
            print(f"\n本轮扫描失败: {type(exc).__name__}: {exc}")
            if args.once:
                return 1
        if args.once:
            return 0
        print(f"\n{cfg.poll_seconds} 秒后重新扫描，Ctrl+C 停止。")
        try:
            time.sleep(cfg.poll_seconds)
        except KeyboardInterrupt:
            print("\n已手动停止。")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
