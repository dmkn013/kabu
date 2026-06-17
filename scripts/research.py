#!/usr/bin/env python3
"""
Stage 1 スクリーニング（トーナメント方式）。
update_ohlcv.py から連鎖起動される。

1. プライム全銘柄をランダムに 100 銘柄ずつのグループに分割
2. 各グループの 20 日 OHLCV を Claude に渡し、有望な上位 N 銘柄を選抜
3. 全グループの生存銘柄を data/shortlist.json に出力（翌朝 decide.py が Stage 2 で使用）

レート制限は claude_agent.invoke_claude_cli が自動リトライする。

  uv run python scripts/research.py
  uv run python scripts/research.py --limit-groups 1   # テスト: 1グループだけ
  uv run python scripts/research.py --group-size 20 --survivors 3
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import argparse
import json
import logging
import os
import random
import re
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(Path(__file__).parent))

import claude_agent
import fetch_data
import fetch_topix

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

SHORTLIST_JSON = REPO_ROOT / 'data' / 'shortlist.json'


def next_business_day(d: date) -> date:
    """翌営業日（土日をスキップ）を返す。research は前日夕方に翌営業日分を生成する。"""
    from datetime import timedelta
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5:  # 5=土, 6=日
        nd += timedelta(days=1)
    return nd

# Stage 1 のモデル（スクリーニングは軽量モデルでレート制限を節約）
STAGE1_MODEL = os.environ.get('STAGE1_MODEL', 'claude-sonnet-4-6')

GROUP_SIZE = 100        # 1グループの銘柄数
SURVIVORS = 5           # 各グループからの生存数
LOOKBACK = 20           # Stage 1 で渡す OHLCV 日数
MIN_ROWS = 5            # この日数未満のデータしかない銘柄は除外


def _load_sim_context(today: date) -> dict:
    """アクティブRunの資金・期間情報を集約して返す。"""
    from datetime import timedelta

    config_path = REPO_ROOT / 'config.json'
    runs_json = REPO_ROOT / 'data' / 'runs.json'

    ctx = {
        'min_cash': 500_000,
        'max_position_pct': 0.30,
        'sim_start': today,
        'sim_end': today,
        'total_biz_days': 0,
        'current_biz_day': 0,
    }

    if config_path.exists():
        cfg = json.loads(config_path.read_text(encoding='utf-8'))
        ctx['max_position_pct'] = cfg.get('max_long_position_pct', 0.30)
        ctx['min_cash'] = cfg.get('initial_cash', 500_000)

    if not runs_json.exists():
        return ctx

    runs = json.loads(runs_json.read_text(encoding='utf-8')).get('runs', [])
    active = [r for r in runs if r.get('status') == 'active']
    if not active:
        return ctx

    ctx['sim_start'] = min(date.fromisoformat(r['start_date']) for r in active)
    ctx['sim_end'] = max(date.fromisoformat(r['end_date']) for r in active)

    min_cash = float('inf')
    for r in active:
        pf_path = REPO_ROOT / 'data' / 'runs' / r['id'] / 'portfolio.json'
        if pf_path.exists():
            pf = json.loads(pf_path.read_text(encoding='utf-8'))
            min_cash = min(min_cash, pf.get('cash', ctx['min_cash']))
    if min_cash < float('inf'):
        ctx['min_cash'] = min_cash

    def _count_biz_days(d1: date, d2: date) -> int:
        n, d = 0, d1
        while d <= d2:
            if d.weekday() < 5:
                n += 1
            d += timedelta(days=1)
        return n

    ctx['total_biz_days'] = _count_biz_days(ctx['sim_start'], ctx['sim_end'])
    ctx['current_biz_day'] = _count_biz_days(ctx['sim_start'], today)
    return ctx


def _format_group_ohlcv(
    group: list[str], master: dict[str, dict], max_price: int = 0
) -> tuple[str, list[str]]:
    """グループの OHLCV テキストと、データのある銘柄リストを返す。
    max_price > 0 の場合、最新終値がその金額を超える銘柄を除外する。
    """
    lines = []
    included = []
    skipped_price = 0
    for sym in group:
        df = fetch_data.load_ohlcv_csv(sym)
        if df is None or len(df) < MIN_ROWS:
            continue
        if max_price > 0 and df['Close'].iloc[-1] > max_price:
            skipped_price += 1
            continue
        info = master.get(sym, {})
        name = info.get('name', '')
        sector = info.get('sector', '')
        lines.append(f"{sym} {name} [{sector}]")
        for dt, row in df.tail(LOOKBACK).iterrows():
            dstr = dt.strftime('%m/%d') if hasattr(dt, 'strftime') else str(dt)[:10]
            lines.append(
                f"  {dstr} O={int(row['Open'])} H={int(row['High'])} "
                f"L={int(row['Low'])} C={int(row['Close'])} V={int(row['Volume']):,}"
            )
        included.append(sym)
    if skipped_price:
        logger.debug(f'価格フィルター: {skipped_price} 銘柄を除外 (終値 > ¥{max_price:,})')
    return '\n'.join(lines), included


def _build_stage1_prompt(group_idx: int, ohlcv_text: str, n: int, ctx: dict) -> str:
    min_cash = int(ctx['min_cash'])
    max_pos = int(min_cash * ctx['max_position_pct'])
    max_price = int(max_pos / 100)
    sim_start = ctx['sim_start'].strftime('%Y/%m/%d')
    sim_end = ctx['sim_end'].strftime('%Y/%m/%d')
    total = ctx['total_biz_days']
    current = ctx['current_biz_day']
    remaining = max(0, total - current)

    return f"""あなたは日本株のスクリーニング担当アナリストです。
これは多段階選抜（トーナメント）の Stage 1 です。プライム市場の銘柄を
ランダムに 100 銘柄ずつのグループに分けており、これはグループ {group_idx} です。

## シミュレーション概要
- 目的: 初期資金 ¥{min_cash:,} で日本株の売買シミュレーションを行い、期間終了時の総資産を最大化する
- 運用期間: {sim_start} ～ {sim_end}（全 {total} 営業日）
- 本日: {current} 営業日目（残り {remaining} 営業日）
- 現在の最低可用資金: ¥{min_cash:,}
- 1 ポジション集中上限: {int(ctx['max_position_pct'] * 100)}%（最大 ¥{max_pos:,}）
- **購入可能な株価の上限: ¥{max_price:,}（100株×¥{max_price:,} = ¥{max_pos:,} 以内）**
  ※ データはすでにこの上限でフィルター済みです

## あなたのタスク
以下の銘柄群の直近 {LOOKBACK} 営業日の値動き（OHLCV）を分析し、
**今後数週間で売買の妙味がある（値動きが期待できる）上位 {n} 銘柄**を選抜してください。
ロング（上昇期待）でもショート（下落期待）でも構いません。

## 判断材料
- トレンド（上昇/下降/レンジ）
- 出来高の変化（急増は注目）
- モメンタム・ボラティリティ
- 直近の急騰・急落とその反動余地
- 残り {remaining} 営業日のタイムホライズンに適した値動きか

## 銘柄データ（{LOOKBACK}営業日 OHLCV）
{ohlcv_text}

## 出力フォーマット（最終回答）
分析の最後に、**JSON 配列のみ**を出力してください。余分なテキストは含めないこと。
ちょうど {n} 銘柄（データが乏しく選べない場合はそれ未満でも可）。

```
[
  {{"symbol": "7203", "reason": "出来高急増を伴う上昇トレンド、押し目買い妙味"}},
  {{"symbol": "9984", "reason": "高値圏での失速、反落リスク（ショート候補）"}}
]
```

symbol は上記データに含まれる銘柄コードのみ。reason は簡潔な日本語で。"""


def _parse_survivors(text: str, valid_symbols: set[str]) -> list[dict]:
    """Claude 出力から [{symbol, reason}] を抽出し、グループ内の銘柄のみ残す。"""
    # 1. 直接 JSON
    parsed = None
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        # 2. ```json ブロック
        m = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(1))
            except json.JSONDecodeError:
                parsed = None
        # 3. 最後の [...] 配列
        if parsed is None:
            matches = re.findall(r'(\[[\s\S]*?\])', text)
            for mtext in reversed(matches):
                try:
                    parsed = json.loads(mtext)
                    break
                except json.JSONDecodeError:
                    continue

    if not isinstance(parsed, list):
        logger.warning(f'Stage1 パース失敗。先頭200字: {text[:200]}')
        return []

    result = []
    seen = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        sym = str(item.get('symbol', '')).strip()
        reason = str(item.get('reason', '')).strip()
        if sym in valid_symbols and sym not in seen:
            result.append({'symbol': sym, 'reason': reason})
            seen.add(sym)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit-groups', type=int, default=0, help='処理するグループ数の上限（0=全件）')
    parser.add_argument('--group-size', type=int, default=GROUP_SIZE)
    parser.add_argument('--survivors', type=int, default=SURVIVORS)
    parser.add_argument('--model', default=STAGE1_MODEL)
    args = parser.parse_args()

    today = date.today()
    target = next_business_day(today)         # 翌営業日分を生成
    target_str = target.strftime('%Y-%m-%d')
    logger.info(
        f'[research] {today} 実行 → {target_str} (翌営業日) 分の Stage 1 '
        f'スクリーニング開始 (model={args.model})'
    )

    symbols = fetch_topix.get_prime_symbols(refresh=False)
    master = fetch_topix.load_symbol_master()
    if not symbols:
        logger.error('銘柄リストが空。fetch_topix を先に実行してください')
        return 1

    ctx = _load_sim_context(today)
    max_price = int(ctx['min_cash'] * ctx['max_position_pct'] / 100)
    logger.info(
        f'シミュレーション: {ctx["sim_start"]} ～ {ctx["sim_end"]} '
        f'({ctx["current_biz_day"]}/{ctx["total_biz_days"]} 営業日目) '
        f'最低資金 ¥{int(ctx["min_cash"]):,} → 株価フィルター ¥{max_price:,} 以下'
    )

    # 対象営業日シードでランダムシャッフル（同対象日への再実行で同じグループになる）
    rng = random.Random(int(target.strftime('%Y%m%d')))
    shuffled = symbols[:]
    rng.shuffle(shuffled)

    groups = [shuffled[i:i + args.group_size] for i in range(0, len(shuffled), args.group_size)]
    if args.limit_groups > 0:
        groups = groups[:args.limit_groups]
    logger.info(f'{len(shuffled)} 銘柄を {len(groups)} グループに分割（各最大{args.group_size}銘柄）')

    all_candidates = []
    for gi, group in enumerate(groups, 1):
        ohlcv_text, included = _format_group_ohlcv(group, master, max_price=max_price)
        if not included:
            logger.warning(f'グループ {gi}: データのある銘柄なし、スキップ')
            continue
        logger.info(f'--- グループ {gi}/{len(groups)} ({len(included)} 銘柄) ---')

        prompt = _build_stage1_prompt(gi, ohlcv_text, args.survivors, ctx)
        stdout = claude_agent.invoke_claude_cli(prompt, model=args.model, timeout=600)
        if stdout is None:
            logger.warning(f'グループ {gi}: Claude 呼び出し失敗、スキップ')
            continue

        survivors = _parse_survivors(stdout, set(included))
        for rank, s in enumerate(survivors[:args.survivors], 1):
            sym = s['symbol']
            all_candidates.append({
                'symbol': sym,
                'name': master.get(sym, {}).get('name', ''),
                'sector': master.get(sym, {}).get('sector', ''),
                'reason': s['reason'],
                'group': gi,
                'rank': rank,
            })
        logger.info(f'グループ {gi}: {len(survivors)} 銘柄選抜 → ' +
                    ', '.join(s['symbol'] for s in survivors))

    payload = {'date': target_str, 'count': len(all_candidates), 'candidates': all_candidates}
    tmp = SHORTLIST_JSON.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(SHORTLIST_JSON)
    logger.info(f'[research] 完了: {len(all_candidates)} 銘柄を {SHORTLIST_JSON} に出力')
    return 0


if __name__ == '__main__':
    sys.exit(main())
