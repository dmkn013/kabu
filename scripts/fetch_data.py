import logging
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)


def fetch_ohlcv(symbols: list[str], lookback_days: int = 20) -> dict[str, pd.DataFrame]:
    tickers = [f"{s}.T" for s in symbols]
    period_days = int(lookback_days * 1.8) + 10
    start = (datetime.today() - timedelta(days=period_days)).strftime("%Y-%m-%d")
    end = datetime.today().strftime("%Y-%m-%d")

    try:
        raw = yf.download(
            tickers=tickers,
            start=start,
            end=end,
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        logger.error(f"yfinance download failed: {e}")
        return {}

    if raw.empty:
        logger.warning("yfinance returned empty data")
        return {}

    result: dict[str, pd.DataFrame] = {}

    if len(symbols) == 1:
        sym = symbols[0]
        df = raw.copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        if not df.empty:
            result[sym] = df.tail(lookback_days)
        return result

    for sym, ticker in zip(symbols, tickers):
        try:
            if ticker in raw.columns.get_level_values(1):
                df = raw.xs(ticker, axis=1, level=1).copy()
            else:
                logger.warning(f"{sym}: no data")
                continue
            df = df.dropna(how="all")
            df.index = pd.to_datetime(df.index).tz_localize(None)
            if df.empty:
                continue
            result[sym] = df.tail(lookback_days)
        except Exception as e:
            logger.warning(f"{sym}: error ({e})")

    return result


def get_latest_close(ohlcv: dict[str, pd.DataFrame]) -> dict[str, float]:
    return {sym: float(df['Close'].iloc[-1]) for sym, df in ohlcv.items() if not df.empty}


def get_latest_open(ohlcv: dict[str, pd.DataFrame]) -> dict[str, float]:
    return {sym: float(df['Open'].iloc[-1]) for sym, df in ohlcv.items() if not df.empty}


def fetch_opening_prices_1m(symbols: list[str]) -> dict[str, float]:
    """当日の寄付価格を1分足の第1足始値で取得する。ストップ配慮等で約定なしの場合は該当銘柄を返さない。"""
    from datetime import date as _date
    today = _date.today()
    tickers = [f"{s}.T" for s in symbols]

    try:
        raw = yf.download(
            tickers=tickers,
            period="1d",
            interval="1m",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        logger.error(f"yfinance 1m download failed: {e}")
        return {}

    if raw.empty:
        logger.warning("yfinance 1m returned empty data")
        return {}

    def _first_open(df: pd.DataFrame) -> float | None:
        df = df.dropna(how="all")
        if df.empty:
            return None
        idx = pd.to_datetime(df.index)
        today_rows = df[[ts.date() == today for ts in idx]]
        if today_rows.empty:
            return None
        return float(today_rows.iloc[0]["Open"])

    result: dict[str, float] = {}

    if len(symbols) == 1:
        sym = symbols[0]
        val = _first_open(raw.copy())
        if val is not None:
            result[sym] = val
        return result

    for sym, ticker in zip(symbols, tickers):
        try:
            if ticker not in raw.columns.get_level_values(1):
                logger.warning(f"{sym}: 1m データなし")
                continue
            df = raw.xs(ticker, axis=1, level=1).copy()
            val = _first_open(df)
            if val is not None:
                result[sym] = val
            else:
                logger.warning(f"{sym}: 当日 1m データなし（ストップ配慮等の可能性）")
        except Exception as e:
            logger.warning(f"{sym}: 1m 始値取得エラー ({e})")

    return result


def format_ohlcv_for_prompt(sym: str, df: pd.DataFrame, last_n: int = 5) -> str:
    lines = [f"{sym}:"]
    for dt, row in df.tail(last_n).iterrows():
        date_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)[:10]
        lines.append(
            f"  {date_str}: O={int(row['Open'])} H={int(row['High'])} "
            f"L={int(row['Low'])} C={int(row['Close'])} V={int(row['Volume']):,}"
        )
    return '\n'.join(lines)
