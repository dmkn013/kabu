#!/usr/bin/env python3
"""
Step 1 -- 8:30 実行
全アクティブ RUN に対して Claude が売買判断 -> pending_orders.json + trades.csv WAIT 登録
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import argparse
import csv
import json
import logging
import os
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(Path(__file__).parent))

import claude_agent
import fetch_data
from portfolio import Portfolio

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

RUNS_JSON = REPO_ROOT / 'data' / 'runs.json'
CONFIG_PATH = REPO_ROOT / 'config.json'


def git_push(today_str: str, step: str) -> None:
    import subprocess
    cmds = [
        ['git', 'add', 'data/'],
        ['git', 'commit', '-m', f'[{step}] {today_str}'],
        ['git', 'push'],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        if r.returncode != 0:
            if 'nothing to commit' in r.stdout + r.stderr:
                logger.info('git: 変更なし、push スキップ')
                return
            logger.warning(f'git {cmd[1]} 失敗: {r.stderr[:200]}')
            return
    logger.info('git push 完了')


def load_config() -> dict:
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return json.load(f)


def load_runs() -> list[dict]:
    with open(RUNS_JSON, encoding='utf-8') as f:
        return json.load(f)['runs']


def load_recent_trades(run_dir: Path, n: int = 10) -> list[dict]:
    csv_path = run_dir / 'trades.csv'
    if not csv_path.exists():
        return []
    with open(csv_path, encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    filled = [r for r in rows if r.get('status', '').upper() in ('FILLED', 'UNFILLED')]
    return filled[-n:]


def append_wait_trade(run_dir: Path, today_str: str, order: dict, cash: float) -> None:
    csv_path = run_dir / 'trades.csv'
    row = {
        'date': today_str,
        'time': '08:30',
        'symbol': order['symbol'],
        'action': order['action'],
        'shares': order['shares'],
        'price': '',
        'status': 'WAIT',
        'cash_after': int(cash),
    }
    write_header = not csv_path.exists()
    with open(csv_path, 'a', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['date','time','symbol','action','shares','price','status','cash_after'])
        if write_header:
            w.writeheader()
        w.writerow(row)


def calc_days_remaining(end_date_str: str, today: date) -> tuple[int, int]:
    end_dt = date.fromisoformat(end_date_str)
    calendar_days = max(0, (end_dt - today).days)
    market_days = max(0, round(calendar_days * 5 / 7))
    return calendar_days, market_days


def get_position_prices(pf: Portfolio) -> dict[str, float]:
    """保有中の銘柄だけ価格を取得する（Claude への参照価格）。"""
    syms = list(pf.positions.keys()) + list(pf.short_positions.keys())
    if not syms:
        return {}
    ohlcv = fetch_data.fetch_ohlcv(syms, lookback_days=5)
    return fetch_data.get_latest_close(ohlcv)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help='週末・重複チェックを無視して強制実行')
    args = parser.parse_args()

    today = date.today()
    today_str = today.strftime('%Y-%m-%d')
    logger.info(f'[decide] {today_str} 売買判断ステップ開始' + (' [--force]' if args.force else ''))

    if today.weekday() >= 5 and not args.force:
        logger.info('週末のためスキップ（--force で強制実行可）')
        return 0

    config = load_config()
    runs = load_runs()
    active_runs = [r for r in runs if r['status'] == 'active']

    if not active_runs:
        logger.info('アクティブな RUN がありません')
        return 0

    for run in active_runs:
        run_id = run['id']
        run_dir = REPO_ROOT / 'data' / 'runs' / run_id
        logger.info(f'--- {run_id} ({run["name"]}) ---')

        pending_path = run_dir / 'pending_orders.json'
        if pending_path.exists() and not args.force:
            existing = json.loads(pending_path.read_text(encoding='utf-8'))
            if existing.get('date') == today_str:
                logger.info(f'{run_id}: 本日の意思決定済みスキップ')
                continue

        calendar_days, market_days = calc_days_remaining(run['end_date'], today)
        logger.info(f'{run_id}: 終了まで残り約{market_days}営業日（暦日{calendar_days}日）')

        pf = Portfolio(str(run_dir / 'portfolio.json'))

        # 保有ポジションがある場合のみ参照価格を取得
        reference_prices = get_position_prices(pf)

        recent_trades = load_recent_trades(run_dir)

        logger.info(f'{run_id}: Claude ({claude_agent.MODEL}) に売買判断を依頼中...')
        decisions = claude_agent.get_trading_decisions(
            cash=pf.cash,
            positions=pf.positions,
            short_positions=pf.short_positions,
            reference_prices=reference_prices,
            recent_trades=recent_trades,
            config=config,
            days_remaining=calendar_days,
            market_days_remaining=market_days,
        )

        pending_path.write_text(
            json.dumps({'date': today_str, 'orders': decisions}, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

        for order in decisions:
            append_wait_trade(run_dir, today_str, order, pf.cash)
            logger.info(f"  WAIT: {order['action']} {order['symbol']} {order['shares']}株 limit={order['limit_price']}")

        if not decisions:
            logger.info(f'{run_id}: 判断なし (HOLD)')
        else:
            logger.info(f'{run_id}: {len(decisions)} 件 WAIT 登録')

    git_push(today_str, 'decide')
    return 0


if __name__ == '__main__':
    sys.exit(main())
