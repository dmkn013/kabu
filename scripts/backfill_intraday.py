#!/usr/bin/env python3
"""
intraday.csv を遡及生成する。
trades.csv からポートフォリオ状態を再構築し、
yfinance の30分足データから評価額を算出して intraday.csv に補完する。

  uv run python scripts/backfill_intraday.py
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import csv, json, logging, subprocess
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).parent.parent
JST = ZoneInfo('Asia/Tokyo')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)


def load_trades(run_dir: Path) -> list[dict]:
    with open(run_dir / 'trades.csv', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def reconstruct_states(trades: list[dict], initial_cash: float) -> dict[str, tuple]:
    """各日の execute 後のポートフォリオ状態を再構築する。
    Returns: {date_str: (cash, {sym: shares}, {sym: shares})}
    """
    cash = initial_cash
    longs: dict[str, int] = {}
    shorts: dict[str, int] = {}
    states: dict[str, tuple] = {}

    by_date: dict[str, list] = defaultdict(list)
    for t in trades:
        by_date[t['date']].append(t)

    for d in sorted(by_date.keys()):
        for t in by_date[d]:
            if t['status'] != 'FILLED':
                continue
            sym = t['symbol']
            shares = int(t['shares'])
            action = t['action'].upper()
            cash = float(t['cash_after'])

            if action == 'BUY':
                longs[sym] = longs.get(sym, 0) + shares
            elif action == 'SELL':
                longs[sym] = longs.get(sym, 0) - shares
                if longs[sym] <= 0:
                    longs.pop(sym, None)
            elif action == 'SHORT':
                shorts[sym] = shorts.get(sym, 0) + shares
            elif action == 'COVER':
                shorts[sym] = shorts.get(sym, 0) - shares
                if shorts[sym] <= 0:
                    shorts.pop(sym, None)

        states[d] = (cash, dict(longs), dict(shorts))

    return states


def fetch_close_prices(symbols: list[str], start: str, end: str) -> pd.DataFrame:
    """yfinance 30分足の Close 価格を JST タイムゾーン付きで返す。
    Returns DataFrame: index=JST datetime, columns=symbol (without .T)
    """
    tickers = [f'{s}.T' for s in symbols]
    df = yf.download(tickers, start=start, end=end, interval='30m',
                     progress=False, auto_adjust=True)
    if df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        close = df['Close'].copy()
        close.columns = [c.removesuffix('.T') for c in close.columns]
    else:
        close = df[['Close']].copy()
        close.columns = symbols

    if close.index.tz is None:
        close.index = close.index.tz_localize('UTC').tz_convert(JST)
    else:
        close.index = close.index.tz_convert(JST)

    return close


def load_intraday(intraday_path: Path) -> list[dict]:
    if not intraday_path.exists():
        return []
    with open(intraday_path, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write_intraday(intraday_path: Path, rows: list[dict]) -> None:
    rows.sort(key=lambda r: r['datetime'])
    seen: set[str] = set()
    deduped = []
    for r in rows:
        if r['datetime'] not in seen:
            seen.add(r['datetime'])
            deduped.append(r)

    with open(intraday_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(
            f, fieldnames=['datetime', 'cash', 'long_value', 'short_exposure', 'total_value']
        )
        writer.writeheader()
        writer.writerows(deduped)

    logger.info(f'  → {intraday_path.name}: {len(deduped)} rows')


def main() -> None:
    runs_data = json.loads((REPO_ROOT / 'data' / 'runs.json').read_text(encoding='utf-8'))

    backfill_dates = ['2026-06-22', '2026-06-23', '2026-06-24']
    fetch_start = '2026-06-22'
    fetch_end = '2026-06-25'

    for run in runs_data.get('runs', []):
        if run.get('status') != 'active':
            continue

        run_id = run['id']
        run_dir = REPO_ROOT / 'data' / 'runs' / run_id
        intraday_path = run_dir / 'intraday.csv'
        initial_cash = float(run.get('initial_cash', 500000))

        logger.info(f'=== {run_id} ===')

        trades = load_trades(run_dir)
        states = reconstruct_states(trades, initial_cash)

        # 全期間で必要な銘柄を収集
        all_syms: set[str] = set()
        for d in backfill_dates:
            if d in states:
                _, longs, shorts = states[d]
                all_syms.update(longs)
                all_syms.update(shorts)

        if not all_syms:
            logger.info('  ポジションなし、スキップ')
            continue

        logger.info(f'  銘柄: {sorted(all_syms)}')
        prices_df = fetch_close_prices(list(all_syms), fetch_start, fetch_end)
        if prices_df.empty:
            logger.warning('  価格データ取得失敗')
            continue

        existing_rows = load_intraday(intraday_path)
        existing_dts = {r['datetime'] for r in existing_rows}
        logger.info(f'  既存 intraday: {len(existing_rows)} rows')

        new_rows: list[dict] = []
        for d in backfill_dates:
            if d not in states:
                logger.warning(f'  {d}: state なし、スキップ')
                continue

            cash, longs, shorts = states[d]
            day = date.fromisoformat(d)
            day_start = datetime(day.year, day.month, day.day, 9, 0, tzinfo=JST)
            day_end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=JST)

            day_df = prices_df[
                (prices_df.index >= day_start) & (prices_df.index <= day_end)
            ]

            count = 0
            for ts, price_row in day_df.iterrows():
                dt_str = ts.strftime('%Y-%m-%d %H:%M')
                if dt_str in existing_dts:
                    continue

                prices: dict[str, float] = {}
                for sym in list(longs) + list(shorts):
                    if sym in price_row.index:
                        v = price_row[sym]
                        if pd.notna(v) and float(v) > 0:
                            prices[sym] = float(v)

                long_val = sum(sh * prices.get(sym, 0) for sym, sh in longs.items())
                short_exp = sum(sh * prices.get(sym, 0) for sym, sh in shorts.items())

                if long_val == 0 and short_exp == 0:
                    continue

                new_rows.append({
                    'datetime': dt_str,
                    'cash': int(cash),
                    'long_value': int(long_val),
                    'short_exposure': int(short_exp),
                    'total_value': int(cash + long_val - short_exp),
                })
                count += 1

            logger.info(f'  {d}: {count} 件追加')

        if new_rows:
            write_intraday(intraday_path, existing_rows + new_rows)
        else:
            logger.info('  追加なし')

    # git commit & push
    subprocess.run(['git', 'add', 'data/'], check=False, cwd=str(REPO_ROOT))
    result = subprocess.run(
        ['git', 'commit', '-m', '[backfill] intraday 6/22-6/24 遡及生成'],
        check=False, cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    out = (result.stdout + result.stderr).strip()
    logger.info(out or '(no output)')
    subprocess.run(['git', 'push'], check=False, cwd=str(REPO_ROOT))
    logger.info('完了')


if __name__ == '__main__':
    main()
