import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
OHLCV_DIR = REPO_ROOT / 'data' / 'ohlcv'
OHLCV_COLS = ['Open', 'High', 'Low', 'Close', 'Volume']


# ---------------------------------------------------------------------------
# 銘柄ごとの日次 OHLCV CSV キャッシュ（data/ohlcv/{symbol}.csv）
# ---------------------------------------------------------------------------

def ohlcv_csv_path(symbol: str) -> Path:
    return OHLCV_DIR / f'{symbol}.csv'


def save_ohlcv_csv(symbol: str, df: pd.DataFrame) -> None:
    """OHLCV を data/ohlcv/{symbol}.csv に保存する。index=date。"""
    OHLCV_DIR.mkdir(parents=True, exist_ok=True)
    out = df[[c for c in OHLCV_COLS if c in df.columns]].copy()
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    out = out[~out.index.duplicated(keep='last')].sort_index()
    out.index.name = 'date'
    out.to_csv(ohlcv_csv_path(symbol))


def load_ohlcv_csv(symbol: str) -> pd.DataFrame | None:
    """data/ohlcv/{symbol}.csv を読み込む。無ければ None。"""
    path = ohlcv_csv_path(symbol)
    if not path.exists():
        return None
    df = pd.read_csv(path, index_col='date', parse_dates=['date'])
    return df


def upsert_ohlcv_csv(symbol: str, new_df: pd.DataFrame) -> None:
    """既存 CSV に new_df をマージして保存する（日付重複は新しい方を採用）。"""
    existing = load_ohlcv_csv(symbol)
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, new_df])
    else:
        merged = new_df
    save_ohlcv_csv(symbol, merged)


def download_daily(
    symbols: list[str], start: str, end: str, chunk: int = 100
) -> dict[str, pd.DataFrame]:
    """複数銘柄の日次 OHLCV をチャンク分割して取得する（raw 価格 / auto_adjust=False）。

    返り値は {symbol: DataFrame(index=date, cols=OHLCV_COLS)}。
    end は yfinance では排他的なので呼び出し側で +1 日しておくこと。
    """
    result: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        tickers = [f'{s}.T' for s in batch]
        try:
            raw = yf.download(
                tickers=tickers,
                start=start,
                end=end,
                interval='1d',
                auto_adjust=False,
                progress=False,
                threads=True,
                group_by='column',
            )
        except Exception as e:
            logger.error(f'download_daily chunk {i // chunk} 失敗: {e}')
            continue

        if raw is None or raw.empty:
            continue

        if len(batch) == 1:
            sym = batch[0]
            df = raw.copy()
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            df = df.dropna(how='all')
            if not df.empty:
                result[sym] = df[[c for c in OHLCV_COLS if c in df.columns]]
            continue

        for sym, ticker in zip(batch, tickers):
            try:
                if ticker not in raw.columns.get_level_values(1):
                    continue
                df = raw.xs(ticker, axis=1, level=1).copy()
                df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
                df = df.dropna(how='all')
                if df.empty:
                    continue
                result[sym] = df[[c for c in OHLCV_COLS if c in df.columns]]
            except Exception as e:
                logger.warning(f'{sym}: 解析エラー ({e})')

        logger.info(f'  取得 {i + len(batch)}/{len(symbols)} 銘柄...')

    return result


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


def fetch_opening_prices_1m(symbols: list[str], max_retries: int = 3, retry_wait: int = 60) -> dict[str, float]:
    """寄付価格を取得する。yf.Ticker.fast_info.open を使用。
    市場オープン直後はキャッシュが追いつかないことがあるため、
    取得できなかった銘柄は retry_wait 秒待ってリトライする。"""
    import time
    result: dict[str, float] = {}
    remaining = list(symbols)

    for attempt in range(1, max_retries + 1):
        still_missing = []
        for sym in remaining:
            try:
                fi = yf.Ticker(f"{sym}.T").fast_info
                price = fi.open
                if price and price > 0:
                    result[sym] = float(price)
                    logger.info(f"{sym}: 始値 {price:.0f} (fast_info, attempt={attempt})")
                else:
                    logger.warning(f"{sym}: fast_info.open が None/0 (attempt={attempt})")
                    still_missing.append(sym)
            except Exception as e:
                logger.warning(f"{sym}: fast_info 取得エラー ({e}) (attempt={attempt})")
                still_missing.append(sym)

        remaining = still_missing
        if not remaining:
            break
        if attempt < max_retries:
            logger.info(f"{len(remaining)} 銘柄が未取得。{retry_wait}秒後にリトライ ({attempt}/{max_retries})...")
            time.sleep(retry_wait)

    if remaining:
        logger.error(f"始値取得失敗: {remaining}")
    if not result:
        logger.error("全銘柄の始値取得失敗")
    return result
