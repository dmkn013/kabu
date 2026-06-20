#!/usr/bin/env python3
"""
日中スナップショット。前場終了後(11:35)・後場終了後(15:35)に実行。
保有ポジションの現在値を取得し intraday.csv に追記、market_prices.json を更新する。

  uv run python scripts/snapshot.py
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import json, logging, os, subprocess
from datetime import date, datetime
from pathlib import Path

import yfinance as yf

REPO_ROOT = Path(__file__).parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(Path(__file__).parent))
from portfolio import Portfolio

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def fetch_current_prices(symbols: list[str]) -> dict[str, float]:
    prices: dict[str, float] = {}
    for sym in symbols:
        try:
            fi = yf.Ticker(f'{sym}.T').fast_info
            price = getattr(fi, 'last_price', None) or getattr(fi, 'open', None)
            if price and float(price) > 0:
                prices[sym] = float(price)
                logger.info(f'{sym}: ¥{price:.0f}')
            else:
                logger.warning(f'{sym}: 価格取得失敗')
        except Exception as e:
            logger.warning(f'{sym}: {e}')
    return prices


def main() -> int:
    today = date.today()
    if today.weekday() >= 5:
        logger.info('週末のためスキップ')
        return 0

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    logger.info(f'[snapshot] {now_str} 開始')

    runs_path = REPO_ROOT / 'data' / 'runs.json'
    if not runs_path.exists():
        logger.error('runs.json が見つかりません')
        return 1
    runs_data = json.loads(runs_path.read_text(encoding='utf-8'))

    all_symbols: set[str] = set()
    run_portfolios: dict[str, Portfolio] = {}

    for run in runs_data.get('runs', []):
        if run.get('status') != 'active':
            continue
        run_dir = REPO_ROOT / 'data' / 'runs' / run['id']
        pf_path = run_dir / 'portfolio.json'
        if not pf_path.exists():
            continue
        pf = Portfolio(str(pf_path))
        run_portfolios[run['id']] = pf
        all_symbols.update(pf.positions.keys())
        all_symbols.update(pf.short_positions.keys())

    if not all_symbols:
        logger.info('保有ポジションなし - スキップ')
        return 0

    prices = fetch_current_prices(list(all_symbols))
    if not prices:
        logger.error('全銘柄の現在値取得失敗')
        return 1

    # market_prices.json 更新（フロントエンドで現在値表示に使用）
    mp_path = REPO_ROOT / 'data' / 'market_prices.json'
    mp_path.write_text(
        json.dumps({'updated_at': now_str, 'prices': prices}, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )

    # 各 Run の intraday.csv に追記
    header = 'datetime,cash,long_value,short_exposure,total_value\n'
    for run_id, pf in run_portfolios.items():
        run_dir = REPO_ROOT / 'data' / 'runs' / run_id
        summary = pf.get_summary(prices)

        row = (
            f"{now_str},{int(summary['cash'])},{int(summary['long_value'])},"
            f"{int(summary['short_exposure'])},{int(summary['total_value'])}\n"
        )

        intraday_path = run_dir / 'intraday.csv'
        if not intraday_path.exists():
            intraday_path.write_text(header + row, encoding='utf-8')
        else:
            with open(intraday_path, 'a', encoding='utf-8', newline='') as f:
                f.write(row)

        logger.info(
            f'{run_id}: 現金¥{int(summary["cash"]):,} / '
            f'ロング¥{int(summary["long_value"]):,} / '
            f'ショート¥{int(summary["short_exposure"]):,} / '
            f'総資産¥{int(summary["total_value"]):,}'
        )

    # git commit + push
    subprocess.run(['git', 'add', 'data/'], check=False, cwd=str(REPO_ROOT))
    subprocess.run(
        ['git', 'commit', '-m', f'[snapshot] {now_str}'],
        check=False, cwd=str(REPO_ROOT),
    )
    subprocess.run(['git', 'push'], check=False, cwd=str(REPO_ROOT))

    logger.info('[snapshot] 完了')
    return 0


if __name__ == '__main__':
    sys.exit(main())
