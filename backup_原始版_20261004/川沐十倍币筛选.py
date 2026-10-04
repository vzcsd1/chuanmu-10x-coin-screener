from pathlib import Path
import logging
import json
import math
import sys

import binance_box_strategy as base


def scan(cfg):
    spot, futures, cfg = base.connect_exchanges(cfg)
    okx = base.okx_exchange(cfg)
    tickers = spot.fetch_tickers()
    ftickers = futures.fetch_tickers()
    try:
        okx_markets = okx.load_markets()
    except Exception:
        okx_markets = {}
    caps = {}
    if cfg.use_coingecko:
        try: caps = base.coingecko_caps(cfg)
        except Exception as exc: logging.warning("CoinGecko unavailable: %s", exc)
    rows = []
    for symbol in base.select_symbols(tickers, cfg):
        try:
            market = spot.market(symbol); base_name = market["base"]
            future = next((s for s,m in futures.markets.items() if m.get("base")==base_name and m.get("swap") and m.get("linear") and m.get("quote")=="USDT"), None)
            if not future: continue
            candles = spot.fetch_ohlcv(symbol, cfg.timeframe, limit=cfg.lookback)
            df = base.indicators(candles, cfg)
            if len(df) < cfg.box_period + 3: continue
            ticker = tickers[symbol]; ft = ftickers.get(future, {})
            cap = caps.get(base_name); volume = base._as_float(ticker.get("quoteVolume")); ratio = volume/cap if cap else None
            oi = futures.fetch_open_interest(future); oi_value = base._as_float(oi.get("openInterestValue")) or base._as_float(oi.get("openInterestAmount"))*base._as_float(ft.get("last"))
            oi_ratio = oi_value/cap if cap and oi_value else None
            funding = futures.fetch_funding_rate(future); funding_rate = base._as_float(funding.get("fundingRate"), float("nan"))
            okx_contract = any(m.get("base")==base_name and m.get("quote")=="USDT" and m.get("swap") for m in okx_markets.values())
            closed = df.iloc[:-1]; low48, high48 = closed["low"].tail(48).min(), closed["high"].tail(48).max()
            move48 = (high48-low48)/low48 if low48 else float("nan")
            v4 = closed["volume"].tail(4).sum(); prev4 = closed["volume"].tail(8).head(4).sum(); v24 = closed["volume"].tail(24).sum(); prev24 = closed["volume"].tail(48).head(24).sum()
            details = []
            if math.isfinite(funding_rate) and funding_rate >= 0: details.append("资金费率>=0")
            if ratio is not None and ratio >= .60: details.append("成交量/市值>=60%")
            if oi_ratio is not None and oi_ratio >= 1: details.append("OI/市值>=1")
            if okx_contract: details.append("OKX有合约")
            if 0.15 < move48 < 0.25: details.append("48h波动15%-25%")
            if v4 > prev4: details.append("4h量增")
            if v24 > prev24: details.append("24h量增")
            rows.append({"symbol":symbol,"market_cap":cap,"spot_quote_volume":volume,"futures_quote_volume":base._as_float(ft.get("quoteVolume")),"oi_cap_ratio":oi_ratio,"volume_cap_ratio":ratio,"funding_rate":funding_rate,"conditions_met":len(details),"conditions":",".join(details) or "无","percentage_24h":base._as_float(ticker.get("percentage"),float("nan"))})
        except Exception:
            logging.exception("failed %s", symbol)
    top = {r["symbol"] for r in sorted(rows,key=lambda x:x.get("percentage_24h",float("-inf")),reverse=True)[:10]}
    for r in rows:
        if r["symbol"] in top: r["conditions"] += ",涨幅前十"; r["conditions_met"] += 1
        r["score"] = r["conditions_met"]
    return sorted([r for r in rows if r["conditions_met"] >= 4], key=lambda x:x["score"], reverse=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = base.env_config(); rows = scan(cfg)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
