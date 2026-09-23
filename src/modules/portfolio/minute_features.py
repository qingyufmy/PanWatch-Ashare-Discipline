"""P6: source-timestamped CN 1m bars, local feature computation and parquet archive."""

from __future__ import annotations

import concurrent.futures
import uuid
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

from src.modules.portfolio.discipline import logical_hash

SH = ZoneInfo("Asia/Shanghai")
INTERVALS = (5, 15, 30, 60, 120)
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="minute-source")
_COLUMNS = ("symbol", "timestamp", "open", "high", "low", "close", "volume", "amount", "source")


def _source_frame(symbol: str, source: str, trade_date: str) -> pd.DataFrame:
    import akshare as ak

    if source == "sina":
        raw = ak.stock_zh_a_minute(symbol=symbol, period="1", adjust="")
        raw = raw.rename(columns={"day": "timestamp"})
    elif source == "eastmoney":
        raw = ak.stock_zh_a_hist_min_em(
            symbol=symbol[2:], period="1", adjust="",
            start_date=f"{trade_date} 09:30:00", end_date=f"{trade_date} 15:01:00",
        ).rename(columns={"时间": "timestamp", "开盘": "open", "最高": "high",
                          "最低": "low", "收盘": "close", "成交量": "volume", "成交额": "amount"})
    else:
        raise ValueError("minute_source_unknown")
    if raw.empty:
        raise ValueError("minute_source_empty")
    raw = raw.copy()
    raw["symbol"] = symbol
    raw["source"] = source
    return normalize_bars(raw, trade_date)


def normalize_bars(raw: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """Preserve vendor asof; never substitute local fetch time for market time."""
    missing = set(_COLUMNS) - set(raw.columns)
    if missing:
        raise ValueError(f"minute_columns_missing:{sorted(missing)}")
    frame = raw.loc[:, list(_COLUMNS)].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    if frame["timestamp"].dt.tz is None:
        frame["timestamp"] = frame["timestamp"].dt.tz_localize(SH)
    else:
        frame["timestamp"] = frame["timestamp"].dt.tz_convert(SH)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame[frame.timestamp.dt.strftime("%Y-%m-%d") == trade_date]
    frame = frame[frame.timestamp.dt.time.apply(
        lambda t: time(9, 31) <= t <= time(11, 30) or time(13, 1) <= t <= time(15, 0)
    )]
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    frame = frame[(frame.open > 0) & (frame.high >= frame[["open", "close", "low"]].max(axis=1))
                  & (frame.low <= frame[["open", "close", "high"]].min(axis=1))
                  & (frame.volume >= 0)]
    frame = frame.sort_values("timestamp").drop_duplicates(["symbol", "timestamp"], keep="last")
    if frame.empty:
        raise ValueError("minute_bars_invalid_or_empty")
    frame["source_hash"] = [logical_hash({col: str(row[col]) for col in _COLUMNS})
                            for _, row in frame.iterrows()]
    frame["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return frame.reset_index(drop=True)


def fetch_minute_bars(symbol: str, trade_date: str, *, timeout_seconds: int = 12,
                      sources=("sina", "eastmoney"), fetcher=_source_frame) -> tuple[pd.DataFrame, list[dict]]:
    if not isinstance(symbol, str) or len(symbol) != 8 or symbol[:2] not in {"sh", "sz", "bj"} or not symbol[2:].isdigit():
        raise ValueError("invalid_cn_symbol")
    failures = []
    for source in sources:
        future = _POOL.submit(fetcher, symbol, source, trade_date)
        try:
            frame = future.result(timeout=timeout_seconds)
            if frame.empty:
                raise ValueError("minute_source_empty")
            return frame, failures
        except Exception as exc:
            future.cancel()
            failures.append({"source": source, "error": type(exc).__name__, "detail": str(exc)[:150]})
    raise RuntimeError(f"all_minute_sources_failed:{failures}")


def missing_minutes(frame: pd.DataFrame) -> list[str]:
    day = frame.timestamp.iloc[0].tz_convert(SH).date()
    expected = list(pd.date_range(f"{day} 09:31", f"{day} 11:30", freq="min", tz=SH))
    expected += list(pd.date_range(f"{day} 13:01", f"{day} 14:57", freq="min", tz=SH))
    expected += [pd.Timestamp(f"{day} 15:00", tz=SH)]
    observed = set(frame.timestamp)
    latest = frame.timestamp.max()
    return [t.isoformat() for t in expected if t <= latest and t not in observed]


def aggregate_bars(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes not in INTERVALS:
        raise ValueError("unsupported_interval")
    rows = []
    frame = frame.sort_values("timestamp")
    for (_, session), part in frame.groupby([
        frame.timestamp.dt.date,
        frame.timestamp.dt.hour.lt(12).map({True: "AM", False: "PM"}),
    ]):
        for start in range(0, len(part), minutes):
            chunk = part.iloc[start:start + minutes]
            rows.append({"symbol": chunk.symbol.iloc[-1], "timestamp": chunk.timestamp.iloc[-1],
                         "open": chunk.open.iloc[0], "high": chunk.high.max(),
                         "low": chunk.low.min(), "close": chunk.close.iloc[-1],
                         "volume": chunk.volume.sum(), "amount": chunk.amount.sum(),
                         "count": len(chunk), "complete": len(chunk) == minutes,
                         "session": session})
    return pd.DataFrame(rows)


def _indicators(bars: pd.DataFrame) -> dict:
    close, high, low = bars.close.astype(float), bars.high.astype(float), bars.low.astype(float)
    volume = bars.volume.astype(float)
    def value(series):
        last = series.iloc[-1] if isinstance(series, pd.Series) else series
        return round(float(last), 6) if pd.notna(last) else None

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    delta = close.diff()
    gains = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    losses = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + gains / losses.replace(0, float("nan")))
    rsi = rsi.fillna(100 if gains.iloc[-1] > 0 else 50)
    ll = low.rolling(9, min_periods=1).min()
    hh = high.rolling(9, min_periods=1).max()
    rsv = ((close - ll) / (hh - ll).replace(0, float("nan")) * 100).fillna(50)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    amount = bars.amount.astype(float)
    vwap = amount.sum() / volume.sum() if volume.sum() > 0 else None
    return {"ma5": value(close.rolling(5, min_periods=1).mean()),
            "ma20": value(close.rolling(20, min_periods=1).mean()),
            "vwap": round(float(vwap), 6) if vwap else None,
            "macd_dif": value(dif), "macd_dea": value(dea),
            "macd_hist": value((dif - dea) * 2), "rsi14": value(rsi),
            "kdj_k": value(k), "kdj_d": value(d), "kdj_j": value(3 * k - 2 * d),
            "atr14": value(tr.rolling(14, min_periods=1).mean()),
            "volume_ratio": round(float(volume.iloc[-1] / volume.iloc[-6:-1].mean()), 6)
            if len(volume) >= 6 and volume.iloc[-6:-1].mean() > 0 else None,
            "return_pct": round(float((close.iloc[-1] / close.iloc[0] - 1) * 100), 6),
            "amplitude_pct": round(float((high.max() - low.min()) / close.iloc[0] * 100), 6),
            "support_20": value(low.tail(20).min()),
            "resistance_20": value(high.tail(20).max())}


def feature_snapshot(frame: pd.DataFrame) -> dict:
    if frame.empty:
        raise ValueError("minute_bars_empty")
    frame = frame.sort_values("timestamp")
    features = {"1m": _indicators(frame)}
    for interval in INTERVALS:
        bars = aggregate_bars(frame, interval)
        features[f"{interval}m"] = {**_indicators(bars),
                                     "last_bar_complete": bool(bars.complete.iloc[-1])}
    return {"symbol": str(frame.symbol.iloc[-1]), "trade_date": frame.timestamp.iloc[-1].date().isoformat(),
            "market_data_asof": frame.timestamp.iloc[-1].isoformat(),
            "market_data_source": str(frame.source.iloc[-1]),
            "source_hash": logical_hash(frame.source_hash.tolist()),
            "bar_count": len(frame), "missing_minutes": missing_minutes(frame),
            "latest_close": float(frame.close.iloc[-1]), "features": features}


def write_parquet(frame: pd.DataFrame, root: Path) -> Path:
    trade_date = frame.timestamp.iloc[-1].date().isoformat()
    symbol = str(frame.symbol.iloc[-1])
    if any(frame.symbol != symbol) or any(frame.timestamp.dt.date.astype(str) != trade_date):
        raise ValueError("mixed_symbol_or_date")
    path = root / f"trade_date={trade_date}" / f"symbol={symbol}" / "bars.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"bars.{uuid.uuid4().hex}.tmp.parquet")
    frame.to_parquet(temp, index=False)
    temp.replace(path)
    return path


def read_parquet(path: Path) -> pd.DataFrame:
    with duckdb.connect(":memory:") as conn:
        return conn.execute("SELECT * FROM read_parquet(?) ORDER BY timestamp", [str(path)]).df()
