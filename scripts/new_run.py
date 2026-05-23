#!/usr/bin/env python3
"""
新しい RUN を作成する。
Usage: uv run python scripts/new_run.py --name "Run 1"
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import argparse
import csv
import json
import os
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
os.chdir(REPO_ROOT)

RUNS_JSON = REPO_ROOT / 'data' / 'runs.json'
CONFIG_PATH = REPO_ROOT / 'config.json'


def load_runs() -> list[dict]:
    if not RUNS_JSON.exists():
        return []
    with open(RUNS_JSON, encoding='utf-8') as f:
        return json.load(f).get('runs', [])


def save_runs(runs: list[dict]) -> None:
    RUNS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUNS_JSON.with_suffix('.tmp')
    tmp.write_text(json.dumps({'runs': runs}, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(RUNS_JSON)


def next_run_id(runs: list[dict]) -> str:
    if not runs:
        return 'run_001'
    nums = []
    for r in runs:
        try:
            nums.append(int(r['id'].split('_')[1]))
        except (IndexError, ValueError):
            pass
    return f"run_{(max(nums) + 1):03d}"


def main() -> None:
    parser = argparse.ArgumentParser(description='新しいシミュレーション RUN を作成する')
    parser.add_argument('--name', required=True, help='RUN の表示名 (例: "Run 1")')
    parser.add_argument('--start', default=None, help='開始日 YYYY-MM-DD (省略時: 今日)')
    parser.add_argument('--days', type=int, default=30, help='シミュレーション日数 (市場営業日, デフォルト: 30)')
    args = parser.parse_args()

    with open(CONFIG_PATH, encoding='utf-8') as f:
        config = json.load(f)

    initial_cash = config.get('initial_cash', 500000)
    today = date.today().isoformat()
    start_date = args.start or today

    from datetime import timedelta
    start_dt = date.fromisoformat(start_date)
    end_dt = start_dt + timedelta(days=int(args.days * 1.5) + 10)
    end_date = end_dt.isoformat()

    runs = load_runs()
    run_id = next_run_id(runs)
    run_dir = REPO_ROOT / 'data' / 'runs' / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    portfolio = {
        'cash': initial_cash,
        'positions': {},
        'short_positions': {},
        'last_updated': None,
        'initial_cash': initial_cash,
    }
    (run_dir / 'portfolio.json').write_text(
        json.dumps(portfolio, ensure_ascii=False, indent=2), encoding='utf-8'
    )

    with open(run_dir / 'trades.csv', 'w', encoding='utf-8', newline='') as f:
        csv.DictWriter(f, fieldnames=['date','time','symbol','action','shares','price','status','cash_after']).writeheader()

    with open(run_dir / 'daily_summary.csv', 'w', encoding='utf-8', newline='') as f:
        csv.DictWriter(f, fieldnames=['date','cash','long_value','short_exposure','total_value']).writeheader()

    run_meta = {
        'id': run_id,
        'name': args.name,
        'status': 'active',
        'created_at': today,
        'start_date': start_date,
        'end_date': end_date,
        'initial_cash': initial_cash,
    }
    runs.append(run_meta)
    save_runs(runs)

    print(f'RUN 作成完了: {run_id} ({args.name})')
    print(f'  ディレクトリ: {run_dir}')
    print(f'  開始日: {start_date}  終了日: {end_date}')
    print(f'  初期資金: ¥{initial_cash:,}')


if __name__ == '__main__':
    main()
