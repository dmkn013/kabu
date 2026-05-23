#!/usr/bin/env python3
"""
Step 1 -- 8:30 実行
全アクティブ RUN に対して Claude が売買判断 -> pending_orders.json + trades.csv WAIT 登録
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

RUNS_JSON = REPO_ROOT / 'data' / 'runs.json'
CONFIG_PATH = REPO_ROOT / 'config.json'


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


def main() -> int:
    today = date.today()
    today_str = today.strftime('%Y-%m-%d')
    logger.info(f'[decide] {today_str} 売買判断ステップ開始')

    if today.weekday() >= 5:
        logger.info('週末のためスキップ')
        return 0

    config = load_config()
    runs = load_runs()
    active_runs = [r for r in runs if r['status'] == 'active']

    if not active_runs:
        logger.info('アクティブな RUN がありません')
        return 0

    logger.info(f'株価データ取得中 (共通)...')
    ohlcv = fetch_data.fetch_ohlcv(config['stocks'], config['lookback_days'])
    if not ohlcv:
        logger.error('株価データ取得失敗')
        return 1

    reference_prices = fetch_data.get_latest_close(ohlcv)

    for run in active_runs:
        run_id = run['id']
        run_dir = REPO_ROOT / 'data' / 'runs' / run_id
        logger.info(f'--- {run_id} ({run["name"]}) ---')

        pending_path = run_dir / 'pending_orders.json'
        if pending_path.exists():
            existing = json.loads(pending_path.read_text(encoding='utf-8'))
            if existing.get('date') == today_str:
                logger.info(f'{run_id}: 本日の意思決定済みスキップ')
                continue

        pf = Portfolio(str(run_dir / 'portfolio.json'))
        recent_trades = load_recent_trades(run_dir)

        logger.info(f'{run_id}: Claude に売買判断を依頼中...')
        decisions = claude_agent.get_trading_decisions(
            cash=pf.cash,
            positions=pf.positions,
            short_positions=pf.short_positions,
            ohlcv_data=ohlcv,
            recent_trades=recent_trades,
            current_prices=reference_prices,
            config=config,
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
