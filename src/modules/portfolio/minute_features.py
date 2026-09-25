"""P6: source-timestamped CN 1m bars, local feature computation and parquet archive."""

from __future__ import annotations

import concurrent.futures
import math
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
FEATURE_VERSION = "minute-v2"
SOURCE_CONTRACTS = {
    "sina": {"volume_unit": "share", "amount_unit": "CNY", "price_basis": "unadjusted", "timestamp_semantics": "bar_end"},
    "eastmoney": {"volume_unit": "lot", "amount_unit": "CNY", "price_basis": "unadjusted", "timestamp_semantics": "bar_end"},
    "fixture": {"volume_unit": "share", "amount_unit": "CNY", "price_basis": "unadjusted", "timestamp_semantics": "bar_end"},
}


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
    sources = set(frame["source"].dropna().astype(str))
    if len(sources) != 1 or next(iter(sources), None) not in SOURCE_CONTRACTS:
        raise ValueError("minute_source_contract_unknown_or_mixed")
    source = next(iter(sources))
    contract = SOURCE_CONTRACTS[source]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
    if frame["timestamp"].dt.tz is None:
        frame["timestamp"] = frame["timestamp"].dt.tz_localize(SH)
    else:
        frame["timestamp"] = frame["timestamp"].dt.tz_convert(SH)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame[col] = frame[col].replace([float("inf"), float("-inf")], float("nan"))
    frame["raw_volume"] = frame["volume"]
    if contract["volume_unit"] == "lot":
        frame["volume"] *= 100
    frame["volume_unit"] = "share"
    frame["source_volume_unit"] = contract["volume_unit"]
    frame["amount_unit"] = contract["amount_unit"]
    frame["price_basis"] = contract["price_basis"]
    frame["timestamp_semantics"] = contract["timestamp_semantics"]
    frame["normalization_version"] = FEATURE_VERSION
    frame = frame[frame.timestamp.dt.strftime("%Y-%m-%d") == trade_date]
    frame = frame[frame.timestamp.dt.time.apply(
        lambda t: time(9, 31) <= t <= time(11, 30) or time(13, 1) <= t <= time(15, 0)
    )]
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    frame = frame[(frame.open > 0) & (frame.high >= frame[["open", "close", "low"]].max(axis=1))
                  & (frame.low <= frame[["open", "close", "high"]].min(axis=1))
                  & (frame.volume >= 0)]
    # A CNY/share VWAP far outside the observed bar is a broken unit/source row.
    implied = frame["amount"] / frame["volume"].replace(0, float("nan"))
    invalid = (frame["amount"].notna() & (frame["amount"] < 0)) | (
        (frame["volume"] == 0) & frame["amount"].notna() & (frame["amount"] != 0)
    ) | (
        (frame["volume"] > 0) & frame["amount"].notna()
        & ((implied < frame["low"] * 0.90) | (implied > frame["high"] * 1.10))
    )
    frame = frame[~invalid]
    frame = frame.sort_values("timestamp").drop_duplicates(["symbol", "timestamp"], keep="last")
    if frame.empty:
        raise ValueError("minute_bars_invalid_or_empty")
    frame["source_hash"] = [logical_hash({col: str(row[col]) for col in (*_COLUMNS, "raw_volume", "normalization_version")})
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
    for (day, session), part in frame.groupby([
        frame.timestamp.dt.date,
        frame.timestamp.dt.hour.lt(12).map({True: "AM", False: "PM"}),
    ]):
        session_start = pd.Timestamp(f"{day} {'09:31' if session == 'AM' else '13:01'}", tz=SH)
        # The closing auction may be reported only at 15:00. Never invent 14:58/59.
        offsets = ((part.timestamp - session_start).dt.total_seconds() // 60).astype(int)
        for bucket, chunk in part.groupby(offsets // minutes, sort=True):
            expected = [session_start + pd.Timedelta(minutes=int(bucket) * minutes + i)
                        for i in range(minutes)]
            observed = set(chunk.timestamp)
            missing = [t.isoformat() for t in expected if t not in observed]
            auction_gap = session == "PM" and any(t.strftime("%H:%M") in {"14:58", "14:59"} for t in expected)
            rows.append({"symbol": chunk.symbol.iloc[-1], "timestamp": expected[-1],
                         "open": chunk.open.iloc[0], "high": chunk.high.max(),
                         "low": chunk.low.min(), "close": chunk.close.iloc[-1],
                         "volume": chunk.volume.sum(), "amount": chunk.amount.sum(min_count=1),
                         "source_bar_ids": chunk.source_hash.tolist() if "source_hash" in chunk else [t.isoformat() for t in chunk.timestamp],
                         "count": len(chunk), "complete": not missing,
                         "missing_minutes": missing, "auction_structural_gap": auction_gap and bool(missing),
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
    valid_amount = amount.notna().all() and (amount >= 0).all()
    vwap = amount.sum() / volume.sum() if valid_amount and volume.sum() > 0 else None
    return {"ma5": value(close.rolling(5, min_periods=5).mean()),
            "ma20": value(close.rolling(20, min_periods=20).mean()),
            "vwap": round(float(vwap), 6) if vwap is not None and math.isfinite(vwap) else None,
            "macd_dif": value(dif) if len(close) >= 26 else None,
            "macd_dea": value(dea) if len(close) >= 35 else None,
            "macd_hist": value((dif - dea) * 2) if len(close) >= 35 else None,
            "rsi14": value(rsi) if len(close) >= 15 else None,
            "kdj_k": value(k) if len(close) >= 9 else None,
            "kdj_d": value(d) if len(close) >= 9 else None,
            "kdj_j": value(3 * k - 2 * d) if len(close) >= 9 else None,
            "atr14": value(tr.rolling(14, min_periods=14).mean()),
            "volume_ratio": round(float(volume.iloc[-1] / volume.iloc[-6:-1].mean()), 6)
            if len(volume) >= 6 and volume.iloc[-6:-1].mean() > 0 else None,
            "volume_ratio_basis": "previous_5_closed_bars",
            "return_pct": round(float((close.iloc[-1] / close.iloc[0] - 1) * 100), 6),
            "amplitude_pct": round(float((high.max() - low.min()) / close.iloc[0] * 100), 6),
            "support_20": value(low.iloc[-21:-1].min()) if len(low) >= 21 else None,
            "resistance_20": value(high.iloc[-21:-1].max()) if len(high) >= 21 else None}


def feature_snapshot(frame: pd.DataFrame, *, decision_time: datetime | None = None,
                     history: pd.DataFrame | None = None) -> dict:
    if frame.empty:
        raise ValueError("minute_bars_empty")
    frame = frame.sort_values("timestamp")
    if decision_time is not None:
        cutoff = pd.Timestamp(decision_time)
        cutoff = cutoff.tz_localize(SH) if cutoff.tzinfo is None else cutoff.tz_convert(SH)
        frame = frame[frame.timestamp <= cutoff]
        if frame.empty:
            raise ValueError("no_closed_minute_bars")
    prior = history if history is not None and not history.empty else pd.DataFrame(columns=frame.columns)
    if not prior.empty:
        if any(prior.symbol != frame.symbol.iloc[0]) or any(prior.timestamp.dt.date >= frame.timestamp.iloc[0].date()):
            raise ValueError("future_or_mixed_history")
        if "price_basis" not in prior or any(prior.price_basis != frame.price_basis.iloc[0]):
            raise ValueError("history_price_basis_mismatch")
    combined = (pd.concat([prior, frame], ignore_index=True) if not prior.empty else frame.copy())
    combined["timestamp"] = pd.to_datetime(combined["timestamp"], utc=True).dt.tz_convert(SH)
    combined = combined.sort_values("timestamp")
    today_indicator = _indicators(frame)
    features = {"1m": {**_indicators(combined), **{
        key: today_indicator[key] for key in ("vwap", "return_pct", "amplitude_pct")
    }}}
    def quality_for(count: int, last_closed_at: str | None) -> dict:
        return {"ma20": "OK" if count >= 20 else "INSUFFICIENT_HISTORY",
                "macd": "OK" if count >= 35 else "INSUFFICIENT_HISTORY",
                "rsi14": "OK" if count >= 15 else "INSUFFICIENT_HISTORY",
                "kdj": "OK" if count >= 9 else "INSUFFICIENT_HISTORY",
                "atr14": "OK" if count >= 14 else "INSUFFICIENT_HISTORY",
                "closed_bars": count, "last_bar_closed_at": last_closed_at}

    quality = {"1m": quality_for(len(combined), combined.timestamp.iloc[-1].isoformat())}
    for interval in INTERVALS:
        bars = aggregate_bars(combined, interval)
        closed = bars[bars.complete]
        if decision_time is not None:
            closed = closed[closed.timestamp <= cutoff]
        indicators = _indicators(closed) if not closed.empty else {
            "ma5": None, "ma20": None, "vwap": None,
            "macd_dif": None, "macd_dea": None, "macd_hist": None,
            "rsi14": None, "kdj_k": None, "kdj_d": None, "kdj_j": None,
            "atr14": None, "volume_ratio": None,
            "volume_ratio_basis": "previous_5_closed_bars", "return_pct": None,
            "amplitude_pct": None, "support_20": None, "resistance_20": None,
        }
        today_closed = closed[closed.timestamp.dt.date == frame.timestamp.iloc[-1].date()]
        if not today_closed.empty:
            today_values = _indicators(today_closed)
            indicators.update({key: today_values[key] for key in ("vwap", "return_pct", "amplitude_pct")})
        else:
            indicators.update({"vwap": None, "return_pct": None, "amplitude_pct": None})
        features[f"{interval}m"] = {**indicators,
                                     "last_bar_complete": bool(bars.complete.iloc[-1])}
        quality[f"{interval}m"] = quality_for(len(closed), closed.timestamp.iloc[-1].isoformat() if len(closed) else None)
    return {"symbol": str(frame.symbol.iloc[-1]), "trade_date": frame.timestamp.iloc[-1].date().isoformat(),
            "market_data_asof": frame.timestamp.iloc[-1].isoformat(),
            "market_data_source": str(frame.source.iloc[-1]),
            "source_hash": logical_hash(combined.source_hash.tolist()),
            "bar_count": len(frame), "history_bar_count": len(prior),
            "missing_minutes": missing_minutes(frame),
            "latest_close": float(frame.close.iloc[-1]), "features": features,
            "quality": quality, "feature_version": FEATURE_VERSION,
            "source_contract": SOURCE_CONTRACTS.get(str(frame.source.iloc[-1]))}


def load_historical_bars(root: Path, symbol: str, trade_date: str, *, max_days: int = 25) -> pd.DataFrame:
    """Use only archived prior sessions; old vendor-unit archives are re-normalized."""
    paths = sorted(root.glob(f"trade_date=*/symbol={symbol}/bars.parquet"), reverse=True)
    frames = []
    for path in paths:
        day = path.parent.parent.name.removeprefix("trade_date=")
        if day >= trade_date:
            continue
        try:
            old = read_parquet(path)
            if old.empty or set(old.source.astype(str)) - set(SOURCE_CONTRACTS):
                continue
            if "normalization_version" in old and (old.normalization_version == FEATURE_VERSION).all():
                old["timestamp"] = pd.to_datetime(old["timestamp"], utc=True).dt.tz_convert(SH)
                normalized = old
            else:
                normalized = normalize_bars(old, day)
            frames.append(normalized)
        except (ValueError, KeyError, TypeError):
            continue
        if len(frames) >= max_days:
            break
    return pd.concat(frames, ignore_index=True).sort_values("timestamp") if frames else pd.DataFrame()


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
