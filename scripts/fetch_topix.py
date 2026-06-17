#!/usr/bin/env python3
"""
JPX 公式の上場銘柄一覧（data_j.xls）からプライム市場（内国株式）の
全銘柄コードを取得するユーティリティ。

- download_symbol_master(): JPX から最新の銘柄マスタを取得
- get_prime_symbols(): プライム内国株式の銘柄コード一覧を返す（キャッシュ対応）
- CLI: `python fetch_topix.py` で data/topix_symbols.json を更新
"""
import io
import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
SYMBOLS_JSON = REPO_ROOT / 'data' / 'topix_symbols.json'

JPX_URL = (
    'https://www.jpx.co.jp/markets/statistics-equities/misc/'
    'tvdivq0000001vg2-att/data_j.xls'
)
_HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

# data_j.xls の列は位置で参照する（Shift-JIS の列名は環境により文字化けするため）
COL_CODE = 1      # コード
COL_NAME = 2      # 銘柄名
COL_MARKET = 3    # 市場・商品区分
COL_SECTOR = 5    # 33業種区分

# 対象とする市場区分（内国株式のプライム市場）
TARGET_MARKET_KEYS = ('プライム', '内国')


def _normalize_code(raw) -> str:
    """コードを4桁の文字列に正規化する。英数字コード（例 130A）も保持。"""
    if isinstance(raw, float):
        if raw.is_integer():
            return str(int(raw))
        return str(raw)
    if isinstance(raw, int):
        return str(raw)
    return str(raw).strip()


def download_symbol_master() -> list[dict]:
    """JPX から銘柄マスタをダウンロードし、プライム内国株式のリストを返す。

    各要素は {'code', 'name', 'sector'} の dict。
    """
    logger.info(f'JPX 銘柄マスタ取得中: {JPX_URL}')
    r = requests.get(JPX_URL, headers=_HEADERS, timeout=60)
    r.raise_for_status()
    df = pd.read_excel(io.BytesIO(r.content))

    market_col = df.columns[COL_MARKET]
    mask = df[market_col].astype(str).apply(
        lambda v: all(k in v for k in TARGET_MARKET_KEYS)
    )
    prime = df[mask]

    result: list[dict] = []
    for _, row in prime.iterrows():
        code = _normalize_code(row.iloc[COL_CODE])
        result.append({
            'code': code,
            'name': str(row.iloc[COL_NAME]).strip(),
            'sector': str(row.iloc[COL_SECTOR]).strip(),
        })
    logger.info(f'プライム内国株式: {len(result)} 銘柄')
    return result


def save_symbols_json(path: Path = SYMBOLS_JSON) -> list[dict]:
    """銘柄マスタを取得して JSON に保存する。"""
    master = download_symbol_master()
    payload = {
        'updated_at': date.today().strftime('%Y-%m-%d'),
        'count': len(master),
        'symbols': master,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)
    logger.info(f'保存完了: {path} ({len(master)} 銘柄)')
    return master


def get_prime_symbols(refresh: bool = False) -> list[str]:
    """プライム内国株式の銘柄コード一覧を返す。

    refresh=True またはキャッシュが無い場合は JPX から再取得する。
    """
    if refresh or not SYMBOLS_JSON.exists():
        master = save_symbols_json()
    else:
        data = json.loads(SYMBOLS_JSON.read_text(encoding='utf-8'))
        master = data.get('symbols', [])
    return [m['code'] for m in master]


def load_symbol_master() -> dict[str, dict]:
    """キャッシュから code -> {name, sector} の辞書を返す。"""
    if not SYMBOLS_JSON.exists():
        return {}
    data = json.loads(SYMBOLS_JSON.read_text(encoding='utf-8'))
    return {m['code']: m for m in data.get('symbols', [])}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    master = save_symbols_json()
    print(f'{len(master)} 銘柄を {SYMBOLS_JSON} に保存しました')
    for m in master[:5]:
        print(f"  {m['code']}  {m['name']}  ({m['sector']})")
