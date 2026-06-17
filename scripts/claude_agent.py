import json
import logging
import os
import re
import subprocess
import time

logger = logging.getLogger(__name__)

# Stage 2（decide.py の最終売買判断）で使うモデル
MODEL = 'claude-opus-4-7'

# レート制限検知時のリトライ設定（環境変数で上書き可）
RETRY_WAIT_SEC = int(os.environ.get('CLAUDE_RETRY_WAIT', '1800'))   # 30分
MAX_RETRIES = int(os.environ.get('CLAUDE_MAX_RETRIES', '12'))       # 最大6時間分

# Claude Code CLI がレート制限/使用上限に達したときに出力する文字列
# （公式エラーリファレンス: https://code.claude.com/docs/en/errors）
RATE_LIMIT_PATTERNS = (
    "hit your session limit",
    "hit your weekly limit",
    "hit your opus limit",
    "hit your sonnet limit",
    "server is temporarily limiting requests",
    "request rejected (429)",
    "rate limit reached",
    "rate limit exceeded",
)


def _is_rate_limited(text: str) -> bool:
    low = text.lower()
    return any(p in low for p in RATE_LIMIT_PATTERNS)


def invoke_claude_cli(
    prompt: str,
    model: str = MODEL,
    timeout: int = 600,
    max_retries: int = MAX_RETRIES,
    retry_wait: int = RETRY_WAIT_SEC,
) -> str | None:
    """`claude -p` を呼び出し、stdout を返す。

    レート制限/使用上限を検知した場合は retry_wait 秒待機して最大 max_retries 回
    リトライする。失敗時は None を返す。

    プロンプトは stdin 経由で渡す（Windows のコマンドライン長制限 32767 文字を回避）。
    """
    cmd = ['claude', '-p', '--model', model, '--dangerously-skip-permissions']

    for attempt in range(1, max_retries + 1):
        logger.info(f'Claude ({model}) を呼び出し中... (試行 {attempt}/{max_retries})')
        try:
            result = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                timeout=timeout, encoding='utf-8',
            )
        except FileNotFoundError:
            logger.error('`claude` コマンドが見つかりません')
            return None
        except subprocess.TimeoutExpired:
            logger.error(f'Claude CLI タイムアウト ({timeout}秒)')
            return None
        except Exception as e:
            logger.error(f'Claude CLI エラー: {e}')
            return None

        combined = (result.stdout or '') + (result.stderr or '')

        if result.returncode == 0 and not _is_rate_limited(combined):
            return result.stdout

        if _is_rate_limited(combined):
            logger.warning(
                f'レート制限/使用上限を検知。{retry_wait}秒待機して再試行 '
                f'(試行 {attempt}/{max_retries})'
            )
            if attempt < max_retries:
                time.sleep(retry_wait)
            continue

        # レート制限以外の非ゼロ終了 — リトライしても無意味なので即終了
        logger.error(f'Claude CLI exit {result.returncode}: {result.stderr[:500]}')
        return None

    logger.error(f'レート制限が解消されず {max_retries} 回で打ち切り')
    return None


def get_trading_decisions(
    cash: float,
    positions: dict,
    short_positions: dict,
    reference_prices: dict[str, float],
    recent_trades: list[dict],
    config: dict,
    days_remaining: int = 0,
    market_days_remaining: int = 0,
    candidates: list[dict] | None = None,
    ohlcv_text: str = '',
    timeout: int = 1800,
) -> list[dict]:
    prompt = _build_prompt(
        cash=cash,
        positions=positions,
        short_positions=short_positions,
        reference_prices=reference_prices,
        recent_trades=recent_trades,
        max_long_pct=config.get('max_long_position_pct', 0.30),
        max_short_exp=config.get('max_short_exposure', 250000),
        days_remaining=days_remaining,
        market_days_remaining=market_days_remaining,
        candidates=candidates,
        ohlcv_text=ohlcv_text,
    )

    stdout = invoke_claude_cli(prompt, model=MODEL, timeout=timeout)
    if stdout is None:
        return []

    decisions = _parse_decisions(stdout)
    logger.info(f'Claude 判断: {len(decisions)} 件')
    return decisions


def _build_prompt(
    cash: float,
    positions: dict,
    short_positions: dict,
    reference_prices: dict[str, float],
    recent_trades: list[dict],
    max_long_pct: float,
    max_short_exp: float,
    days_remaining: int,
    market_days_remaining: int,
    candidates: list[dict] | None = None,
    ohlcv_text: str = '',
) -> str:
    # ロングポジション
    if positions:
        long_lines = []
        for sym, pos in positions.items():
            cp = reference_prices.get(sym, pos['avg_price'])
            pnl = (cp - pos['avg_price']) * pos['shares']
            long_lines.append(
                f"  {sym}: {pos['shares']}株  取得¥{pos['avg_price']:,.0f}  現在¥{cp:,.0f}  含み損益¥{pnl:+,.0f}"
            )
        long_str = '\n'.join(long_lines)
    else:
        long_str = '  なし'

    # ショートポジション
    if short_positions:
        short_lines = []
        for sym, pos in short_positions.items():
            cp = reference_prices.get(sym, pos['avg_short_price'])
            pnl = (pos['avg_short_price'] - cp) * pos['shares']
            short_lines.append(
                f"  {sym}: {pos['shares']}株空売り  建値¥{pos['avg_short_price']:,.0f}  現在¥{cp:,.0f}  含み損益¥{pnl:+,.0f}"
            )
        short_str = '\n'.join(short_lines)
    else:
        short_str = '  なし'

    # 総資産
    long_val = sum(pos['shares'] * reference_prices.get(sym, pos['avg_price']) for sym, pos in positions.items())
    short_exp = sum(pos['shares'] * reference_prices.get(sym, pos['avg_short_price']) for sym, pos in short_positions.items())
    total_value = cash + long_val - short_exp

    # 直近取引
    if recent_trades:
        trade_lines = [
            f"  {t.get('date','')} {t.get('symbol','')} {t.get('action','')} "
            f"{t.get('shares','')}株 価格:{t.get('price','-')} {t.get('status','')}"
            for t in recent_trades[-10:]
        ]
        trades_str = '\n'.join(trade_lines)
    else:
        trades_str = '  なし'

    # 残り日数
    if market_days_remaining <= 0:
        remaining_str = '**本日が最終日**。終了後に全ポジションが成行で強制決済される。'
    elif market_days_remaining <= 3:
        remaining_str = f'残り約{market_days_remaining}営業日。まもなく終了 — ポジション清算を計画すること。'
    else:
        remaining_str = f'残り約{market_days_remaining}営業日（暦日{days_remaining}日）'

    # Stage 1 で選抜された候補銘柄（reason 付き）
    if candidates:
        cand_lines = []
        for c in candidates:
            sym = c.get('symbol', '')
            name = c.get('name', '')
            reason = c.get('reason', '')
            cand_lines.append(f"  {sym} {name}: {reason}")
        candidates_str = '\n'.join(cand_lines)
        universe_str = (
            f"以下は事前スクリーニング（Stage 1）で全プライム市場から選抜された "
            f"{len(candidates)} 銘柄の候補リストです。各銘柄には選抜理由が付いています。\n"
            f"この候補の中から本日の売買銘柄を選ぶこと（保有中ポジションの決済は候補外でも可）。\n\n"
            f"### 候補銘柄（選抜理由付き）\n{candidates_str}"
        )
    else:
        universe_str = (
            "本日は事前スクリーニング結果（候補リスト）がありません。\n"
            "**新規エントリー（BUY / SHORT）は行わず**、保有中ポジションの管理"
            "（SELL / COVER / HOLD）のみ判断すること。"
        )

    ohlcv_section = f"\n\n## 候補銘柄の日次OHLCV（直近）\n{ohlcv_text}" if ohlcv_text else ''

    return f"""あなたは日本株シミュレーションの自動トレーダーです。
利用可能なツール（WebSearch、WebFetch 等）を自由に使い、本日の売買判断を行ってください。

## ミッション
初期資金 ¥500,000 からスタートし、シミュレーション終了時点の総資産を最大化する。
{remaining_str}

## 重要ルール
- シミュレーション最終日に全ポジション（ロング・ショート）が成行で強制決済される
- 残り日数が少ないほど、ポジションを持ち続けることのリスクが増す
- 残り日数を踏まえた戦略を立てること

## 現在の資産状況
- 現金残高: ¥{cash:,.0f}
- 総資産（参考）: ¥{total_value:,.0f}
- ロングポジション（現物保有）:
{long_str}
- ショートポジション（空売り中）:
{short_str}
- ショート建玉合計（時価）: ¥{short_exp:,.0f} / 上限¥{max_short_exp:,.0f}

## 直近の取引履歴（確定済み）
{trades_str}

## 取引対象
{universe_str}{ohlcv_section}

## 分析方針
- 候補それぞれについて、ファンダメンタルズ（業績、PER、PBR、成長率など）とテクニカル（トレンド、出来高、モメンタムなど）の両面から分析すること
- 今日の市況・マクロ環境・セクタートレンドも考慮すること
- WebSearch / WebFetch ツールを使い、有望な候補について最新のニュース・決算・株価を調べてから判断すること
- 全候補を機械的に調べる必要はない。選抜理由とOHLCVから有望なものを優先して深掘りすること

## 注文の仕組み（寄付き指値）
注文は翌営業日の寄付き時点でのみ約定判定される。約定価格は実際の始値。
- BUY/COVER: 始値 ≤ limit_price → 約定（安く買いたい）
- SELL/SHORT: 始値 ≥ limit_price → 約定（高く売りたい）
- 条件を満たさない場合は即キャンセル（翌日に再判断）

## 制約条件
- 株数は 100 株単位
- BUY: 1銘柄のポジションが総資産の {max_long_pct*100:.0f}% を超えないこと（集中リスク管理）
- SHORT: ショート建玉合計が ¥{max_short_exp:,.0f} を超えないこと
- BUY/COVER: 必要額が現金残高 ¥{cash:,.0f} 以内であること
- SELL: 保有株数の範囲内
- COVER: ショート建玉の範囲内

## 出力フォーマット（最終回答）
分析とツール使用が完了したら、**最後の出力として JSON 配列のみ**を返すこと。
余分なテキスト・マークダウン・説明は一切含めないこと。

```
[
  {{"symbol": "7203", "action": "BUY",   "shares": 100, "limit_price": 2850}},
  {{"symbol": "9984", "action": "SHORT", "shares": 100, "limit_price": 9200}},
  {{"symbol": "6758", "action": "SELL",  "shares": 100, "limit_price": 3500}}
]
```

action は BUY / SELL / SHORT / COVER のいずれか。HOLD はリストに含めない。
判断がない場合は空配列 [] を返す。
symbol は東証銘柄コード（4桁数字）。
"""


def _parse_decisions(text: str) -> list[dict]:
    text = text.strip()

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return _validate(data)
    except json.JSONDecodeError:
        pass

    match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, list):
                return _validate(data)
        except json.JSONDecodeError:
            pass

    match = re.search(r'(\[[\s\S]*?\])', text)
    if match:
        try:
            data = json.loads(match.group(1))
            if isinstance(data, list):
                return _validate(data)
        except json.JSONDecodeError:
            pass

    logger.warning(f'JSON パース失敗。先頭300字: {text[:300]}')
    return []


def _validate(raw: list) -> list[dict]:
    valid = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = str(item.get('action', '')).upper()
        symbol = str(item.get('symbol', ''))
        shares = item.get('shares', 0)
        limit_price = item.get('limit_price')

        if action not in ('BUY', 'SELL', 'SHORT', 'COVER'):
            logger.warning(f'不明な action をスキップ: {item}')
            continue
        if not symbol:
            continue
        try:
            shares = int(shares)
            limit_price = float(limit_price)
        except (TypeError, ValueError):
            logger.warning(f'不正な shares/limit_price: {item}')
            continue
        if shares <= 0 or limit_price <= 0:
            continue

        valid.append({'symbol': symbol, 'action': action, 'shares': shares, 'limit_price': limit_price})
    return valid
