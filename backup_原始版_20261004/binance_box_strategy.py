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
    min_quote_volume: float = 5_000_000
    max_symbols: int = 50
    volume_multiplier: float = 1.5
    ema_period: int = 50
    atr_period: int = 14
    stop_atr: float = 1.5
    reward_risk: float = 2.0
    risk_per_trade: float = 0.01
    max_quote_per_trade: float = 100.0
    poll_seconds: int = 300
    min_score: int = 5
    use_coingecko: bool = True
    timeout_ms: int = 30_000
    proxy: str | None = None
    csv_dir: str | None = None
    require_okx: bool = True


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
        min_quote_volume=number("MIN_QUOTE_VOLUME", 5_000_000, float),
        max_symbols=number("MAX_SYMBOLS", 50, int),
        volume_multiplier=number("VOLUME_MULTIPLIER", 1.5, float),
        ema_period=number("EMA_PERIOD", 50, int),
        atr_period=number("ATR_PERIOD", 14, int),
        stop_atr=number("STOP_ATR", 1.5, float),
        reward_risk=number("REWARD_RISK", 2.0, float),
        risk_per_trade=number("RISK_PER_TRADE", 0.01, float),
        max_quote_per_trade=number("MAX_QUOTE_PER_TRADE", 100, float),
        poll_seconds=number("POLL_SECONDS", 300, int),
        min_score=number("MIN_SCORE", 5, int),
        use_coingecko=os.getenv("USE_COINGECKO", "true").lower() == "true",
        timeout_ms=number("REQUEST_TIMEOUT_MS", 30_000, int),
        proxy=resolve_proxy(),
        csv_dir=os.getenv("CSV_DIR") or str(Path(__file__).resolve().parent / "results"),
        require_okx=os.getenv("REQUIRE_OKX", "true").lower() == "true",
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
    """Score note-derived compression, breakout, volume and trend conditions."""
    if len(df) < 2:
        return 0, {}
    row = df.iloc[-2]
    required = ["box_high", "box_low", "avg_volume", "atr", "ema", "close", "volume"]
    if any(column not in df or pd.isna(row[column]) for column in required):
        return 0, {}
    score = 0
    details: dict[str, float] = {}
    width = (row.box_high - row.box_low) / row.close if row.close else 999
    details["box_width_pct"] = float(width * 100)
    if width <= 0.12:
        score += 1
    if row.close > row.box_high:
        score += 2
    if row.volume >= row.avg_volume * cfg.volume_multiplier:
        score += 1
        details["volume_ratio"] = float(row.volume / row.avg_volume)
    if row.close > row.ema:
        score += 1
    closed_volume = df["volume"].iloc[:-1]
    if len(closed_volume) >= 12:
        v0, v1, v2 = (closed_volume.iloc[-4:].sum(), closed_volume.iloc[-8:-4].sum(),
                      closed_volume.iloc[-12:-8].sum())
        details["volume_4h_growth"] = float(v0 / v1) if v1 else 0.0
        if v0 > v1 > v2:
            score += 1
    return score, details


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
    for symbol in select_symbols(tickers, cfg):
        try:
            candles = exchange.fetch_ohlcv(symbol, cfg.timeframe, limit=cfg.lookback)
            df = indicators(candles, cfg)
            if len(df) < cfg.box_period + 3:
                continue
            score, details = box_score(df, cfg)
            trade_levels = signal(symbol, df, cfg) or {}
            base = exchange.market(symbol)["base"]
            future_symbol = future_by_base.get(base)
            if not future_symbol:
                continue
            ft = futures_tickers.get(future_symbol, {})
            quote_volume = _as_float(tickers[symbol].get("quoteVolume"))
            futures_volume = _as_float(ft.get("quoteVolume"))
            if futures_volume >= quote_volume:
                score += 1
            cap = caps.get(base)
            volume_cap_ratio = quote_volume / cap if cap else None
            if cap and cap <= 50_000_000:
                score += 2
            elif cap and cap <= 100_000_000:
                score += 1
            if volume_cap_ratio and volume_cap_ratio >= 0.60:
                score += 2
            try:
                oi = futures.fetch_open_interest(future_symbol)
                oi_value = _as_float(oi.get("openInterestValue"))
                if not oi_value:
                    oi_value = _as_float(oi.get("openInterestAmount")) * _as_float(ft.get("last") or ft.get("close"))
            except Exception:
                oi_value = 0.0
            oi_cap_ratio = oi_value / cap if cap and oi_value else None
            if oi_cap_ratio and oi_cap_ratio >= 0.30:
                score += 1
            if oi_cap_ratio and oi_cap_ratio >= 1.0:
                score += 1
            okx_swap = any(
                market.get("base") == base and market.get("quote") == "USDT"
                and market.get("swap") and market.get("active", True)
                for market in okx_markets.values()
            )
            if okx_swap:
                score += 1
            funding_rate = None
            try:
                funding = futures.fetch_funding_rate(future_symbol)
                funding_rate = _as_float(funding.get("fundingRate"), default=float("nan"))
                if math.isfinite(funding_rate) and funding_rate >= 0:
                    score += 1
            except Exception:
                pass
            percentage = _as_float(tickers[symbol].get("percentage"), default=float("nan"))
            okx_symbol = next((s for s, m in okx_markets.items()
                               if m.get("base") == base and m.get("quote") == "USDT"
                               and m.get("swap")), None)
            okx_volume = _as_float(okx_tickers.get(okx_symbol, {}).get("quoteVolume")) if okx_symbol else 0.0
            hit_conditions = []
            if okx_swap: hit_conditions.append("币安+OKX合约")
            if math.isfinite(percentage) and percentage > 0: hit_conditions.append("上涨")
            if funding_rate is not None and math.isfinite(funding_rate) and funding_rate >= 0: hit_conditions.append("资金费率非负")
            if volume_cap_ratio and volume_cap_ratio >= 0.60: hit_conditions.append("成交量/市值>=60%")
            if oi_cap_ratio and oi_cap_ratio >= 0.30: hit_conditions.append("OI/市值>=30%")
            if oi_cap_ratio and oi_cap_ratio >= 1.0: hit_conditions.append("OI>=市值")
            rows.append({"symbol": symbol, "score": score, "conditions_met": len(hit_conditions),
                         "conditions": ",".join(hit_conditions) or "无", "market_cap": cap,
                         "spot_quote_volume": quote_volume, "futures_quote_volume": futures_volume,
                         "okx_quote_volume": okx_volume, "volume_cap_ratio": volume_cap_ratio,
                         "open_interest_value": oi_value, "oi_cap_ratio": oi_cap_ratio,
                         "funding_rate": funding_rate, "percentage_24h": percentage,
                         "okx_contract": okx_swap, **details,
                         "entry": trade_levels.get("entry"), "stop": trade_levels.get("stop"),
                         "take_profit": trade_levels.get("take_profit"),
                         "breakout": bool(df.iloc[-2].close > df.iloc[-2].box_high)})
        except Exception:
            logging.exception("failed scoring %s", symbol)
    top = sorted(rows, key=lambda x: x.get("percentage_24h", float("-inf")), reverse=True)[:10]
    for row in top:
        row["score"] += 1
        row["top_gainer"] = True
    return sorted(rows, key=lambda x: (x["score"], x["futures_quote_volume"]), reverse=True)


def select_symbols(tickers: dict[str, Any], cfg: Config) -> list[str]:
    candidates = []
    for symbol, ticker in tickers.items():
        if not symbol.endswith("/USDT") or ":" in symbol:
            continue
        # Stablecoin pairs never form a tradable box; they only add noise.
        if symbol.split("/")[0] in STABLECOINS:
            continue
        quote_volume = _as_float(ticker.get("quoteVolume"))
        if quote_volume >= cfg.min_quote_volume:
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
        ("评分", "score", "right", lambda r: str(r["score"])),
        ("命中", "conditions_met", "right", lambda r: str(r.get("conditions_met", 0))),
        ("突破", "breakout", "left", lambda r: "是" if r.get("breakout", False) else "-"),
        ("箱体宽", "box_width_pct", "right", lambda r: f"{r.get('box_width_pct', 0):.1f}%"),
        ("量比", "volume_ratio", "right",
         lambda r: f"{r['volume_ratio']:.2f}" if r.get("volume_ratio") else "-"),
        ("4h量增", "volume_4h_growth", "right",
         lambda r: f"{r['volume_4h_growth']:.2f}" if r.get("volume_4h_growth") else "-"),
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
            print(f"{item['symbol']}: {item.get('conditions', '无')}")
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
    print(f"参数: 周期={cfg.timeframe} 箱体={cfg.box_period} 扫描前={cfg.max_symbols} "
          f"门槛={cfg.min_score} 市值源={'CoinGecko' if cfg.use_coingecko else '关闭'}")
    print("正在拉取行情，约需 30-60 秒...\n")
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
