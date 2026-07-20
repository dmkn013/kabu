#!/usr/bin/env python3
"""
intraday.csv を任意期間で遡及生成する。
trades.csv からポートフォリオ状態を再構築し、yfinance の30分足データから
評価額を算出して intraday.csv の 09:00 単発行を 09:00〜15:30 30分おきに置き換える。

  uv run python scripts/backfill_intraday_range.py --start 2026-06-20 --end 2026-07-20
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import argparse, csv, json, logging, subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta
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


def state_on_or_before(states: dict[str, tuple], d: str):
    """d 以前で最も新しい state を返す（当日の trade がない日向け）。"""
    keys = [k for k in states if k <= d]
    if not keys:
        return None
    return states[max(keys)]


def fetch_close_prices(symbols: list[str], start: str, end: str) -> pd.DataFrame:
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

    logger.info(f'  -> {intraday_path.name}: {len(deduped)} rows')


def business_days(start: date, end: date) -> list[str]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', required=True, help='YYYY-MM-DD (inclusive)')
    ap.add_argument('--end', required=True, help='YYYY-MM-DD (inclusive)')
    args = ap.parse_args()

    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)
    backfill_dates = business_days(start_d, end_d)

    # yfinance 30m interval だけ与えるとその日の分が欠けることがあるので end を +1 日
    fetch_start = args.start
    fetch_end = (end_d + timedelta(days=1)).isoformat()

    runs_data = json.loads((REPO_ROOT / 'data' / 'runs.json').read_text(encoding='utf-8'))

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

        all_syms: set[str] = set()
        for d in backfill_dates:
            st = state_on_or_before(states, d)
            if st:
                _, longs, shorts = st
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
        # 対象期間内の既存 09:00 単発行は置き換えるため除外し、期間外の行だけ残す
        kept_rows = [r for r in existing_rows if not (args.start <= r['datetime'][:10] <= args.end)]
        logger.info(f'  既存 intraday: {len(existing_rows)} rows -> 期間内 {len(existing_rows) - len(kept_rows)} rows を再生成対象として除外')

        new_rows: list[dict] = []
        for d in backfill_dates:
            st = state_on_or_before(states, d)
            if st is None:
                logger.warning(f'  {d}: state なし、スキップ')
                continue
            cash, longs, shorts = st

            day = date.fromisoformat(d)
            day_start = datetime(day.year, day.month, day.day, 9, 0, tzinfo=JST)
            day_end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=JST)

            day_df = prices_df[
                (prices_df.index >= day_start) & (prices_df.index <= day_end)
            ]

            count = 0
            for ts, price_row in day_df.iterrows():
                dt_str = ts.strftime('%Y-%m-%d %H:%M')

                prices: dict[str, float] = {}
                for sym in list(longs) + list(shorts):
                    if sym in price_row.index:
                        v = price_row[sym]
                        if pd.notna(v) and float(v) > 0:
                            prices[sym] = float(v)

                long_val = sum(sh * prices.get(sym, 0) for sym, sh in longs.items())
                short_exp = sum(sh * prices.get(sym, 0) for sym, sh in shorts.items())

                if long_val == 0 and short_exp == 0 and (longs or shorts):
                    continue

                new_rows.append({
                    'datetime': dt_str,
                    'cash': int(cash),
                    'long_value': int(long_val),
                    'short_exposure': int(short_exp),
                    'total_value': int(cash + long_val - short_exp),
                })
                count += 1

            logger.info(f'  {d}: {count} 件生成')

        if new_rows:
            write_intraday(intraday_path, kept_rows + new_rows)
        else:
            logger.info('  追加なし')

    subprocess.run(['git', 'add', 'data/'], check=False, cwd=str(REPO_ROOT))
    result = subprocess.run(
        ['git', 'commit', '-m', f'[backfill] intraday {args.start}~{args.end} 30分足で再生成'],
        check=False, cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    out = (result.stdout + result.stderr).strip()
    logger.info(out or '(no output)')
    subprocess.run(['git', 'push'], check=False, cwd=str(REPO_ROOT))
    logger.info('完了')


if __name__ == '__main__':
    main()
