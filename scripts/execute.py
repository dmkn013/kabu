#!/usr/bin/env python3
"""
Step 2 -- 9:05 実行
全アクティブ RUN の WAIT 注文を寄付価格で約定判定し trades.csv を更新する。
daily_summary.csv 記録、pending_orders.json 削除。
BUY/SHORT は成り行き（始値で即約定）。SELL/COVER は指値条件を確認。
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
    logger.info(f'git push 完了')

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


def save_runs(runs: list[dict]) -> None:
    data = {'runs': runs}
    tmp = RUNS_JSON.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(RUNS_JSON)


def update_trades_csv(run_dir: Path, today_str: str, results: list[dict]) -> None:
    """WAIT 行を FILLED/UNFILLED に更新し time を 09:05 に変更する。"""
    csv_path = run_dir / 'trades.csv'
    if not csv_path.exists():
        return

    result_map: dict[tuple, dict] = {}
    for r in results:
        result_map[(r['symbol'], r['action'])] = r

    rows = []
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        # utf-8-sig が BOM を除去するが fieldnames に残る場合を保険的に正規化
        fieldnames = [h.lstrip('﻿') for h in fieldnames]
        for row in reader:
            row = {k.lstrip('﻿'): v for k, v in row.items()}
            key = (row['symbol'], row['action'])
            if row.get('date') == today_str and row.get('status') == 'WAIT' and key in result_map:
                r = result_map.pop(key)
                row['time'] = '09:05'
                row['price'] = str(int(r['price'])) if r['price'] else ''
                row['status'] = r['status']
                row['cash_after'] = str(int(r['cash_after']))
            rows.append(row)

    import io as _io
    buf = _io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows)
    csv_path.write_text(buf.getvalue(), encoding='utf-8')


def append_force_close_trades(run_dir: Path, today_str: str, trades: list[dict]) -> None:
    csv_path = run_dir / 'trades.csv'
    with open(csv_path, 'a', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['date','time','symbol','action','shares','price','status','cash_after'])
        for t in trades:
            w.writerow({
                'date': today_str,
                'time': '09:05',
                'symbol': t['symbol'],
                'action': t['action'],
                'shares': t['shares'],
                'price': int(t['price']),
                'status': t['status'],
                'cash_after': int(t.get('cash_after', 0)),
            })


def append_daily_summary(run_dir: Path, today_str: str, summary: dict) -> None:
    csv_path = run_dir / 'daily_summary.csv'
    write_header = not csv_path.exists()
    with open(csv_path, 'a', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['date','cash','long_value','short_exposure','total_value'])
        if write_header:
            w.writeheader()
        w.writerow({
            'date': today_str,
            'cash': int(summary['cash']),
            'long_value': int(summary['long_value']),
            'short_exposure': int(summary['short_exposure']),
            'total_value': int(summary['total_value']),
        })


def check_limit(action: str, open_price: float, limit_price: float) -> bool:
    if action in ('BUY', 'COVER'):
        return open_price <= limit_price
    elif action in ('SELL', 'SHORT'):
        return open_price >= limit_price
    return False


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help='週末・休日チェックを無視して強制実行')
    args = parser.parse_args()

    today = date.today()
    today_str = today.strftime('%Y-%m-%d')
    logger.info(f'[execute] {today_str} 約定処理開始（9:05）' + (' [--force]' if args.force else ''))

    if today.weekday() >= 5 and not args.force:
        logger.info('週末のためスキップ（--force で強制実行可）')
        return 0

    config = load_config()
    all_runs = load_runs()
    active_runs = [r for r in all_runs if r['status'] == 'active']

    if not active_runs:
        logger.info('アクティブな RUN がありません')
        return 0

    # 全 RUN の注文銘柄を集める（Claude が任意銘柄を選ぶため都度取得）
    all_syms: set[str] = set()
    for run in all_runs:
        if run['status'] != 'active':
            continue
        pending_path = REPO_ROOT / 'data' / 'runs' / run['id'] / 'pending_orders.json'
        if pending_path.exists():
            pending = json.loads(pending_path.read_text(encoding='utf-8-sig'))
            for o in pending.get('orders', []):
                all_syms.add(o['symbol'])
        # 保有ポジションの銘柄も追加
        pf_path = REPO_ROOT / 'data' / 'runs' / run['id'] / 'portfolio.json'
        if pf_path.exists():
            pf_data = json.loads(pf_path.read_text(encoding='utf-8-sig'))
            all_syms.update(pf_data.get('positions', {}).keys())
            all_syms.update(pf_data.get('short_positions', {}).keys())

    logger.info(f'当日始値取得中（1分足）... 対象銘柄: {sorted(all_syms)}')
    open_prices: dict[str, float] = {}
    if all_syms:
        open_prices = fetch_data.fetch_opening_prices_1m(list(all_syms))
        if not open_prices:
            logger.warning('当日始値未取得 — 全銘柄 UNFILLED になります')

    runs_updated = False

    for run in all_runs:
        if run['status'] != 'active':
            continue

        run_id = run['id']
        run_dir = REPO_ROOT / 'data' / 'runs' / run_id
        logger.info(f'--- {run_id} ({run["name"]}) ---')

        pending_path = run_dir / 'pending_orders.json'
        if not pending_path.exists():
            logger.info(f'{run_id}: 保留注文なし')
            pf = Portfolio(str(run_dir / 'portfolio.json'))
            summary = pf.get_summary(open_prices)
            append_daily_summary(run_dir, today_str, summary)
            continue

        pending = json.loads(pending_path.read_text(encoding='utf-8-sig'))
        if pending.get('date') != today_str:
            logger.warning(f"{run_id}: 保留注文日付不一致 ({pending.get('date')} != {today_str})")
            pending_path.unlink(missing_ok=True)
            continue

        orders = pending.get('orders', [])
        pf = Portfolio(str(run_dir / 'portfolio.json'))

        results = []
        for order in orders:
            sym = order['symbol']
            action = order['action']
            shares = order['shares']
            limit_price = order['limit_price']

            open_price = open_prices.get(sym)
            if open_price is None or open_price <= 0:
                logger.warning(f'  {sym}: 始値取得不可 -> UNFILLED')
                results.append({'symbol': sym, 'action': action, 'price': '', 'status': 'UNFILLED', 'cash_after': pf.cash})
                continue

            # BUY/SHORT は成り行き。SELL/COVER のみ指値条件を確認する。
            if action in ('SELL', 'COVER') and not check_limit(action, open_price, limit_price):
                logger.info(f'  {action} {sym}: 指値条件不成立 (始値{open_price:.0f} / 指値{limit_price:.0f}) -> UNFILLED')
                results.append({'symbol': sym, 'action': action, 'price': open_price, 'status': 'UNFILLED', 'cash_after': pf.cash})
                continue

            if action == 'BUY':
                ok, msg = pf.buy(sym, shares, open_price,
                                 max_long_pct=config.get('max_long_position_pct', 0.30),
                                 current_prices=open_prices)
            elif action == 'SELL':
                ok, msg = pf.sell(sym, shares, open_price)
            elif action == 'SHORT':
                ok, msg = pf.short(sym, shares, open_price,
                                   max_short_exposure=config.get('max_short_exposure', 250000),
                                   current_prices=open_prices)
            elif action == 'COVER':
                ok, msg = pf.cover(sym, shares, open_price)
            else:
                continue

            status = 'FILLED' if ok else 'UNFILLED'
            results.append({'symbol': sym, 'action': action, 'price': open_price, 'status': status, 'cash_after': pf.cash})
            log_msg = f'  {action} {sym} {shares}株 @{open_price:.0f} -> {status}'
            if not ok:
                log_msg += f' ({msg})'
            logger.info(log_msg)

        update_trades_csv(run_dir, today_str, results)

        # 最終日: 全ポジション強制決済
        end_date = date.fromisoformat(run['end_date'])
        if today >= end_date:
            logger.info(f'{run_id}: 最終日 - 全ポジション強制決済')
            closed = pf.force_close_all(open_prices)
            for t in closed:
                t['cash_after'] = pf.cash
            if closed:
                append_force_close_trades(run_dir, today_str, closed)
            run['status'] = 'finished'
            runs_updated = True
            logger.info(f'{run_id}: status -> finished')

        pf.last_updated = today_str
        pf.save()

        summary = pf.get_summary(open_prices)
        append_daily_summary(run_dir, today_str, summary)
        logger.info(
            f'{run_id}: 現金¥{summary["cash"]:,.0f} '
            f'ロング¥{summary["long_value"]:,.0f} '
            f'ショート建玉¥{summary["short_exposure"]:,.0f} '
            f'総資産¥{summary["total_value"]:,.0f} '
            f'損益¥{summary["pnl"]:+,.0f}'
        )

        pending_path.unlink(missing_ok=True)

    if runs_updated:
        save_runs(all_runs)

    git_push(today_str, 'execute')
    logger.info('[execute] 約定処理完了')
    return 0


if __name__ == '__main__':
    sys.exit(main())
