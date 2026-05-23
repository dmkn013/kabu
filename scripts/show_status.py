#!/usr/bin/env python3
"""
ターミナルでポートフォリオ状況を確認する。
Usage:
  uv run python scripts/show_status.py           # 全 RUN 一覧
  uv run python scripts/show_status.py --run run_001  # 特定 RUN の詳細
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import argparse
import csv
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(Path(__file__).parent))

RUNS_JSON = REPO_ROOT / 'data' / 'runs.json'


def load_runs() -> list[dict]:
    with open(RUNS_JSON, encoding='utf-8') as f:
        return json.load(f)['runs']


def load_portfolio(run_dir: Path) -> dict:
    path = run_dir / 'portfolio.json'
    if not path.exists():
        return {}
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load_recent_trades(run_dir: Path, n: int = 10) -> list[dict]:
    path = run_dir / 'trades.csv'
    if not path.exists():
        return []
    with open(path, encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    return rows[-n:]


def fmt(n) -> str:
    return f'¥{int(n):,}'


def show_all_runs(runs: list[dict]) -> None:
    print(f'\n{"="*60}')
    print(f'  株取引シミュレーション RUN 一覧')
    print(f'{"="*60}')
    for run in runs:
        run_dir = REPO_ROOT / 'data' / 'runs' / run['id']
        pf = load_portfolio(run_dir)
        if not pf:
            continue
        cash = pf.get('cash', 0)
        initial = pf.get('initial_cash', 500000)
        long_val = sum(p['shares'] * p['avg_price'] for p in pf.get('positions', {}).values())
        short_exp = sum(p['shares'] * p['avg_short_price'] for p in pf.get('short_positions', {}).values())
        total = cash + long_val - short_exp
        pnl = total - initial
        pnl_pct = pnl / initial * 100 if initial > 0 else 0
        sign = '+' if pnl >= 0 else ''
        print(f"\n  [{run['id']}] {run['name']}  ({run['status']})")
        print(f"    更新日: {pf.get('last_updated', '未実行')}")
        print(f"    現金:   {fmt(cash)}")
        print(f"    総資産: {fmt(total)}  損益: {sign}{fmt(pnl)} ({sign}{pnl_pct:.2f}%)")
    print()


def show_run_detail(run: dict) -> None:
    run_dir = REPO_ROOT / 'data' / 'runs' / run['id']
    pf = load_portfolio(run_dir)
    if not pf:
        print(f"{run['id']}: portfolio.json が見つかりません")
        return

    cash = pf.get('cash', 0)
    initial = pf.get('initial_cash', 500000)
    positions = pf.get('positions', {})
    short_positions = pf.get('short_positions', {})

    long_val = sum(p['shares'] * p['avg_price'] for p in positions.values())
    short_exp = sum(p['shares'] * p['avg_short_price'] for p in short_positions.values())
    total = cash + long_val - short_exp
    pnl = total - initial
    pnl_pct = pnl / initial * 100 if initial > 0 else 0
    sign = '+' if pnl >= 0 else ''

    print(f'\n{"="*60}')
    print(f"  {run['id']} / {run['name']}  ({run['status']})")
    print(f'{"="*60}')
    print(f"  更新日:         {pf.get('last_updated', '未実行')}")
    print(f"  現金残高:       {fmt(cash)}")
    print(f"  ロング評価額:   {fmt(long_val)}")
    print(f"  ショート建玉:   {fmt(short_exp)}")
    print(f"  総資産:         {fmt(total)}")
    print(f"  損益:           {sign}{fmt(pnl)} ({sign}{pnl_pct:.2f}%)")

    if positions:
        print(f'\n  ロングポジション:')
        for sym, pos in positions.items():
            print(f"    {sym}: {pos['shares']}株  取得単価{fmt(pos['avg_price'])}")
    else:
        print('\n  ロングポジション: なし')

    if short_positions:
        print(f'\n  ショートポジション:')
        for sym, pos in short_positions.items():
            print(f"    {sym}: {pos['shares']}株空売り  建値{fmt(pos['avg_short_price'])}")
    else:
        print('\n  ショートポジション: なし')

    trades = load_recent_trades(run_dir, 10)
    if trades:
        print(f'\n  直近取引:')
        for t in reversed(trades):
            price_str = f"@{fmt(t['price'])}" if t.get('price') else '(WAIT)'
            print(f"    {t['date']} {t['symbol']} {t['action']} {t['shares']}株 {price_str} [{t['status']}]")
    print()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', default=None, help='RUN ID (例: run_001)')
    args = parser.parse_args()

    runs = load_runs()
    if not runs:
        print('RUN がありません。new_run.py で作成してください。')
        return

    if args.run:
        matched = [r for r in runs if r['id'] == args.run]
        if not matched:
            print(f'RUN が見つかりません: {args.run}')
            return
        show_run_detail(matched[0])
    else:
        show_all_runs(runs)


if __name__ == '__main__':
    main()
