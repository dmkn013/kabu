#!/usr/bin/env python3
"""
初回セットアップ: プライム市場 全銘柄の日次 OHLCV を取得して
data/ohlcv/{symbol}.csv に保存する。手動で1回だけ実行する。

  uv run python scripts/init_ohlcv.py            # 全銘柄・120日
  uv run python scripts/init_ohlcv.py --days 90
  uv run python scripts/init_ohlcv.py --limit 20 # 先頭20銘柄だけ（テスト用）
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import argparse
import logging
import os
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(Path(__file__).parent))

import fetch_data
import fetch_topix

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=120, help='取得する暦日数（既定120）')
    parser.add_argument('--limit', type=int, default=0, help='先頭N銘柄のみ取得（0=全件）')
    args = parser.parse_args()

    logger.info('プライム市場の銘柄リストを取得中...')
    symbols = fetch_topix.get_prime_symbols(refresh=True)
    if args.limit > 0:
        symbols = symbols[:args.limit]
    logger.info(f'対象銘柄: {len(symbols)} 件')

    today = date.today()
    start = (today - timedelta(days=args.days)).strftime('%Y-%m-%d')
    end = (today + timedelta(days=1)).strftime('%Y-%m-%d')  # end は排他的

    logger.info(f'OHLCV 取得中 ({start} 〜 {today})...')
    data = fetch_data.download_daily(symbols, start=start, end=end)

    saved = 0
    for sym, df in data.items():
        if df.empty:
            continue
        fetch_data.save_ohlcv_csv(sym, df)
        saved += 1

    missing = [s for s in symbols if s not in data]
    logger.info(f'保存完了: {saved} 銘柄  /  データ取得不可: {len(missing)} 銘柄')
    if missing:
        logger.info(f'  取得不可の例: {missing[:10]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
