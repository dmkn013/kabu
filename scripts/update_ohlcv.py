#!/usr/bin/env python3
"""
日次 OHLCV 更新（Claude 不使用）。タスクスケジューラから 17:00 に起動。

1. JPX から最新の銘柄リストを取得
2. 既存銘柄は直近データを追記、新規銘柄は過去120日を取得
3. 完了後 research.py を連鎖呼び出し（Stage 1 スクリーニング）

  uv run python scripts/update_ohlcv.py
  uv run python scripts/update_ohlcv.py --no-research   # researchを起動しない
  uv run python scripts/update_ohlcv.py --limit 20      # テスト用
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import argparse
import logging
import os
import subprocess
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

NEW_SYMBOL_DAYS = 120   # 新規銘柄の初回取得日数
REFRESH_WINDOW = 10     # 既存銘柄の追記ウィンドウ（暦日）


def run_research() -> None:
    """research.py を同じ Python インタプリタで連鎖実行する。"""
    script = REPO_ROOT / 'scripts' / 'research.py'
    logger.info(f'research.py を起動: {script}')
    try:
        subprocess.run([sys.executable, str(script)], check=False)
    except Exception as e:
        logger.error(f'research.py 起動失敗: {e}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-research', action='store_true', help='research.py を起動しない')
    parser.add_argument('--limit', type=int, default=0, help='先頭N銘柄のみ（0=全件）')
    args = parser.parse_args()

    today = date.today()
    logger.info(f'[update_ohlcv] {today} OHLCV 更新開始')

    symbols = fetch_topix.get_prime_symbols(refresh=True)
    if args.limit > 0:
        symbols = symbols[:args.limit]
    logger.info(f'対象銘柄: {len(symbols)} 件')

    existing = [s for s in symbols if fetch_data.ohlcv_csv_path(s).exists()]
    new = [s for s in symbols if s not in existing]
    logger.info(f'既存 {len(existing)} 銘柄 / 新規 {len(new)} 銘柄')

    end = (today + timedelta(days=1)).strftime('%Y-%m-%d')  # end は排他的

    # 既存銘柄: 直近ウィンドウを取得して追記
    if existing:
        start = (today - timedelta(days=REFRESH_WINDOW)).strftime('%Y-%m-%d')
        logger.info(f'既存銘柄の直近データ取得中 ({start} 〜 {today})...')
        data = fetch_data.download_daily(existing, start=start, end=end)
        for sym, df in data.items():
            if not df.empty:
                fetch_data.upsert_ohlcv_csv(sym, df)
        logger.info(f'既存銘柄 更新: {len(data)} 件')

    # 新規銘柄: 過去120日を取得
    if new:
        start = (today - timedelta(days=NEW_SYMBOL_DAYS)).strftime('%Y-%m-%d')
        logger.info(f'新規銘柄の履歴取得中 ({start} 〜 {today})...')
        data = fetch_data.download_daily(new, start=start, end=end)
        for sym, df in data.items():
            if not df.empty:
                fetch_data.save_ohlcv_csv(sym, df)
        logger.info(f'新規銘柄 取得: {len(data)} 件')

    logger.info('[update_ohlcv] OHLCV 更新完了')

    if not args.no_research:
        run_research()

    return 0


if __name__ == '__main__':
    sys.exit(main())
