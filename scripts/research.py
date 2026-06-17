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


def _format_group_ohlcv(group: list[str], master: dict[str, dict]) -> tuple[str, list[str]]:
    """グループの OHLCV テキストと、データのある銘柄リストを返す。"""
    lines = []
    included = []
    for sym in group:
        df = fetch_data.load_ohlcv_csv(sym)
        if df is None or len(df) < MIN_ROWS:
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
    return '\n'.join(lines), included


def _build_stage1_prompt(group_idx: int, ohlcv_text: str, n: int) -> str:
    return f"""あなたは日本株のスクリーニング担当アナリストです。
これは多段階選抜（トーナメント）の Stage 1 です。プライム市場の銘柄を
ランダムに 100 銘柄ずつのグループに分けており、これはグループ {group_idx} です。

## あなたのタスク
以下の銘柄群の直近 {LOOKBACK} 営業日の値動き（OHLCV）を分析し、
**今後数週間で売買の妙味がある（値動きが期待できる）上位 {n} 銘柄**を選抜してください。
ロング（上昇期待）でもショート（下落期待）でも構いません。

## 判断材料
- トレンド（上昇/下降/レンジ）
- 出来高の変化（急増は注目）
- モメンタム・ボラティリティ
- 直近の急騰・急落とその反動余地

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
        ohlcv_text, included = _format_group_ohlcv(group, master)
        if not included:
            logger.warning(f'グループ {gi}: データのある銘柄なし、スキップ')
            continue
        logger.info(f'--- グループ {gi}/{len(groups)} ({len(included)} 銘柄) ---')

        prompt = _build_stage1_prompt(gi, ohlcv_text, args.survivors)
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
