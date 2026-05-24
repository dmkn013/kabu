import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import json
import logging
import re
import subprocess

import pandas as pd

logger = logging.getLogger(__name__)

STOCK_NAMES: dict[str, str] = {
    "7203": "トヨタ自動車",
    "6758": "ソニーグループ",
    "9984": "ソフトバンクグループ",
    "8306": "三菱UFJフィナンシャルG",
    "6981": "村田製作所",
    "9432": "日本電信電話(NTT)",
    "8035": "東京エレクトロン",
    "7267": "本田技研工業",
    "6367": "ダイキン工業",
    "4063": "信越化学工業",
    "8411": "みずほフィナンシャルG",
    "9433": "KDDI",
    "7974": "任天堂",
    "6594": "日本電産(ニデック)",
    "4502": "武田薬品工業",
    "7751": "キヤノン",
    "9022": "東海旅客鉄道(JR東海)",
    "8766": "東京海上ホールディングス",
    "6501": "日立製作所",
    "6954": "ファナック",
}


def get_trading_decisions(
    cash: float,
    positions: dict,
    short_positions: dict,
    ohlcv_data: dict[str, pd.DataFrame],
    recent_trades: list[dict],
    current_prices: dict[str, float],
    config: dict,
    days_remaining: int = 0,
    market_days_remaining: int = 0,
) -> list[dict]:
    prompt = _build_prompt(
        cash=cash,
        positions=positions,
        short_positions=short_positions,
        ohlcv_data=ohlcv_data,
        recent_trades=recent_trades,
        current_prices=current_prices,
        max_long_pct=config.get('max_long_position_pct', 0.30),
        max_short_exp=config.get('max_short_exposure', 250000),
        days_remaining=days_remaining,
        market_days_remaining=market_days_remaining,
    )

    logger.info('Claude CLI を呼び出し中...')
    try:
        result = subprocess.run(
            ['claude', '-p', prompt],
            capture_output=True,
            text=True,
            timeout=180,
            encoding='utf-8',
        )
    except FileNotFoundError:
        logger.error('`claude` コマンドが見つかりません')
        return []
    except subprocess.TimeoutExpired:
        logger.error('Claude CLI タイムアウト (180秒)')
        return []
    except Exception as e:
        logger.error(f'Claude CLI エラー: {e}')
        return []

    if result.returncode != 0:
        logger.error(f'Claude CLI exit {result.returncode}: {result.stderr[:500]}')
        return []

    decisions = _parse_decisions(result.stdout)
    logger.info(f'Claude 判断: {len(decisions)} 件')
    return decisions


def _build_prompt(
    cash: float,
    positions: dict,
    short_positions: dict,
    ohlcv_data: dict[str, pd.DataFrame],
    recent_trades: list[dict],
    current_prices: dict[str, float],
    max_long_pct: float,
    max_short_exp: float,
    days_remaining: int,
    market_days_remaining: int,
) -> str:
    from fetch_data import format_ohlcv_for_prompt

    # ロングポジション
    if positions:
        long_lines = []
        for sym, pos in positions.items():
            cp = current_prices.get(sym, pos['avg_price'])
            pnl = (cp - pos['avg_price']) * pos['shares']
            long_lines.append(
                f"  {sym}({STOCK_NAMES.get(sym, sym)}): {pos['shares']}株 "
                f"取得¥{pos['avg_price']:,.0f} 現在¥{cp:,.0f} 含み損益¥{pnl:+,.0f}"
            )
        long_str = '\n'.join(long_lines)
    else:
        long_str = '  なし'

    # ショートポジション
    if short_positions:
        short_lines = []
        for sym, pos in short_positions.items():
            cp = current_prices.get(sym, pos['avg_short_price'])
            pnl = (pos['avg_short_price'] - cp) * pos['shares']
            short_lines.append(
                f"  {sym}({STOCK_NAMES.get(sym, sym)}): {pos['shares']}株空売り "
                f"建値¥{pos['avg_short_price']:,.0f} 現在¥{cp:,.0f} 含み損益¥{pnl:+,.0f}"
            )
        short_str = '\n'.join(short_lines)
    else:
        short_str = '  なし'

    # OHLCV
    ohlcv_lines = []
    for sym, df in ohlcv_data.items():
        if not df.empty:
            name = STOCK_NAMES.get(sym, sym)
            ohlcv_lines.append(format_ohlcv_for_prompt(f'{sym}({name})', df, last_n=5))
    ohlcv_str = '\n'.join(ohlcv_lines) if ohlcv_lines else '  データなし'

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

    # ショート建玉合計
    current_short_exp = sum(
        pos['shares'] * current_prices.get(sym, pos['avg_short_price'])
        for sym, pos in short_positions.items()
    )

    # 総資産
    long_val = sum(pos['shares'] * current_prices.get(sym, pos['avg_price']) for sym, pos in positions.items())
    total_value = cash + long_val - current_short_exp

    # 残り日数の表現
    if market_days_remaining <= 0:
        remaining_str = '本日が最終日（終了後に全ポジション強制決済）'
    elif market_days_remaining <= 3:
        remaining_str = f'残り約{market_days_remaining}営業日（まもなく終了。ポジションを閉じる準備を）'
    else:
        remaining_str = f'残り約{market_days_remaining}営業日（暦日で約{days_remaining}日）'

    return f"""あなたは日本株シミュレーションの自動トレーダーです。
以下のデータを分析し、本日の注文を JSON 配列のみで返してください。
マークダウンや説明文は不要です。JSON 配列だけを返してください。

## ミッション
初期資金 ¥500,000 から始めて、シミュレーション終了時点の総資産を最大化してください。
{remaining_str}

## 重要ルール
- シミュレーション最終日に全ポジション（ロング・ショート）が強制的に成行で決済される
- 残り日数が少ないほど、未決済ポジションのリスクが増す（タイミングを選べない）
- 残り日数を意識した戦略を立てること

## 現在の資産状況
- 現金残高: ¥{cash:,.0f}
- 総資産（参考）: ¥{total_value:,.0f}
- ロングポジション（現物保有）:
{long_str}
- ショートポジション（空売り中）:
{short_str}
- ショート建玉合計（時価）: ¥{current_short_exp:,.0f} / 上限¥{max_short_exp:,.0f}

## 参考銘柄の株価データ（直近5営業日 OHLCV）
以下のデータは参考として提供しています。取引対象はこれらに限らず、
東証に上場している任意の銘柄を選んで構いません。
{ohlcv_str}

## 直近の取引履歴（確定済み）
{trades_str}

## 出力形式（JSON 配列のみ返す）
[
  {{"symbol": "7203", "action": "BUY",   "shares": 100, "limit_price": 2850}},
  {{"symbol": "9984", "action": "SHORT", "shares": 100, "limit_price": 9200}},
  {{"symbol": "6758", "action": "COVER", "shares": 100, "limit_price": 3500}},
  {{"symbol": "8306", "action": "SELL",  "shares": 100, "limit_price": 1720}}
]

## 注文の仕組み（寄付き指値）
- 注文は翌営業日の寄付き（市場開始）時点でのみ約定判定される
- BUY/COVER: 寄付価格 ≤ limit_price のとき約定（安く買いたい）
- SELL/SHORT: 寄付価格 ≥ limit_price のとき約定（高く売りたい）
- 条件を満たさない場合は即キャンセル（翌日に再判断）
- 約定価格は指値ではなく実際の寄付価格

## 制約条件
- action は BUY / SELL / SHORT / COVER のいずれか（HOLDはリストに含めない）
- 株数は 100 株単位
- symbol は東証銘柄コード（4桁数字）
- BUY: 1銘柄のポジションが総資産の {max_long_pct*100:.0f}% を超えないこと
- SHORT: ショート建玉合計が ¥{max_short_exp:,.0f} を超えないこと
- BUY/COVER に必要な現金は現金残高 ¥{cash:,.0f} 以内
- SELL は保有ポジションの範囲内
- COVER はショートポジションの範囲内
- 判断がない場合は空配列 [] を返す
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

    logger.warning(f'JSON パース失敗。先頭200字: {text[:200]}')
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
