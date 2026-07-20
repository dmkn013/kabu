# 株取引シミュレーション アプリ仕様書

> このファイルが本プロジェクトの仕様の基準です。実装はここに記載された仕様に従います。

---

## 1. 目的

Claude CLI を意思決定エンジンとして使い、日本株の売買をシミュレーションする。

- 初期資金 500,000 円を与える
- 毎営業日 Claude が売買判断を行う
- 市場 30 日分経過後における現金残高を最大化する
- 終了時点で全ポジションを強制決済する
- 全取引を CSV で記録する
- 複数の独立した RUN を同時に走らせることができる

---

## 2. システム構成

```
Windows タスクスケジューラ（平日のみ）
    ├─ 3:00  scripts/healthcheck.py → パイプライン健全性の自律調査・修正
    ├─ 8:30  scripts/decide.py      → Stage 2: 候補銘柄を深掘りして売買判断 → WAIT 登録
    ├─ 9:05  scripts/execute.py     → 全アクティブ RUN の WAIT 注文を寄付価格で約定判定
    └─ 17:00 scripts/update_ohlcv.py → OHLCVキャッシュを差分更新 → research.py を連鎖起動

scripts/fetch_data.py    → yfinance で東証 OHLCV 取得・キャッシュ管理
scripts/fetch_topix.py   → JPX公式からTOPIXプライム銘柄マスタを取得
scripts/portfolio.py     → ポートフォリオ状態管理
scripts/claude_agent.py  → `claude -p` サブプロセス呼び出し（レート制限自動リトライ）
scripts/new_run.py       → 新しい RUN を作成するユーティリティ
scripts/init_ohlcv.py    → 初回のみ: 全銘柄の過去OHLCVを一括ダウンロード

data/
    runs.json                    → 全 RUN のメタデータ一覧
    shortlist.json               → Stage 1 スクリーニング結果（翌営業日付）
    topix_symbols.json           → プライム銘柄マスタ（1564銘柄）
    ohlcv/{symbol}.csv           → 銘柄ごとの日次OHLCVキャッシュ（.gitignore済）
    runs/
        {run_id}/
            portfolio.json       → その RUN のポートフォリオ状態
            trades.csv           → その RUN の取引ログ
            daily_summary.csv    → その RUN の日次サマリ（チャート用）
            pending_orders.json  → 当日の WAIT 注文（execute 後に削除）
logs/
    healthcheck_YYYY-MM-DD.txt   → ヘルスチェックの実行ログ

frontend/index.html     → GitHub Pages で公開（git push で自動デプロイ）
```

---

## 2b. 銘柄選定パイプライン（Stage 1 → Stage 2）

毎営業日の売買判断は 2 段階のスクリーニングで行う。

### Stage 1 — 17:00（update_ohlcv.py → research.py）

1. `update_ohlcv.py` が全プライム銘柄の OHLCV を差分更新
2. `research.py` が自動起動（連鎖）
3. 全 1,564 銘柄をランダムに 100 銘柄ずつのグループに分割
4. 各グループの直近 20 日 OHLCV を Claude Sonnet に渡しトーナメント形式でスクリーニング
5. 各グループから上位 5 銘柄を選抜 → 合計約 73〜90 銘柄
6. 結果を `data/shortlist.json`（**翌営業日付**）に保存

```json
{
  "date": "2026-06-19",
  "count": 73,
  "candidates": [
    { "symbol": "1234", "name": "銘柄名", "sector": "セクター",
      "reason": "選抜理由", "group": 1, "rank": 1 }
  ]
}
```

**価格フィルター**: Stage 1 時点で「最新終値 × 100 > 1ポジション上限（cash × 30%）」の銘柄は除外済み。

### Stage 2 — 8:30（decide.py）

1. `shortlist.json` を読み込み（日付不一致なら新規エントリーなし・既存ポジション管理のみ）
2. 候補銘柄の直近 60 日 OHLCV をキャッシュから読み込む
3. Claude Opus に WebSearch 込みで各候補を深掘り分析させる
4. 最終的な BUY / SELL / SHORT / COVER / HOLD 判断を取得
5. → 以降は通常の decide.py フロー（pending_orders.json / trades.csv WAIT 登録）


---

## 3. RUN の概念

### RUN とは

独立したシミュレーションの単位。各 RUN は：

- 同じ初期資金・銘柄ユニバース・期間から始まる
- Claude が独立して判断する（他の RUN の取引履歴は参照しない）
- 独立したデータディレクトリを持つ

### RUN の管理

`data/runs.json` が全 RUN のインデックスを管理する。

```json
{
  "runs": [
    {
      "id":           "run_001",
      "name":         "Run 1",
      "status":       "active",
      "created_at":   "2026-05-23",
      "start_date":   "2026-05-26",
      "end_date":     "2026-06-26",
      "initial_cash": 500000
    },
    {
      "id":           "run_002",
      "name":         "Run 2",
      "status":       "active",
      "created_at":   "2026-05-23",
      "start_date":   "2026-05-26",
      "end_date":     "2026-06-26",
      "initial_cash": 500000
    }
  ]
}
```

`status` は `active`（実行中）または `finished`（終了）。

### RUN の作成

```powershell
uv run python scripts/new_run.py --name "Run 1"
```

`data/runs/{run_id}/` ディレクトリを作成し、初期 `portfolio.json` を配置し、`runs.json` に追記する。

### decide.py / execute.py の動作

- `status: active` の全 RUN をループ処理する
- OHLCV データは全 RUN で共有（1 回だけ取得）
- Claude の呼び出しは RUN ごとに独立して行う（コンテキストを分離する）

---

## 4. 運用パラメータ

| パラメータ | 値 |
|---|---|
| 初期資金 | 500,000 円（config.json で設定） |
| シミュレーション期間 | RUN ごとに `data/runs.json` で設定（例: 30〜40 市場日） |
| 取引頻度 | 毎営業日 1 回 |
| 対象銘柄 | プライム市場全銘柄から Stage1（research.py）で Claude が動的スクリーニング |
| ロング集中上限 | 総資産の 30%（単一銘柄、config.json で設定） |
| ショート建玉上限 | 合計 250,000 円（全銘柄合算、config.json で設定） |

---

## 5. 注文フロー

### Step 1 — 8:30 （decide.py）

1. yfinance で前日までの OHLCV データを取得（全 RUN 共通）
2. アクティブな各 RUN に対して独立して Claude に売買判断を依頼
3. 決定された注文を `data/runs/{run_id}/pending_orders.json` に保存
4. `data/runs/{run_id}/trades.csv` に **WAIT** ステータスで追記
5. ダッシュボードに各 RUN の WAIT 注文が表示される

### Step 2 — 9:05 （execute.py）

1. アクティブな各 RUN の `pending_orders.json` を読み込む
2. yfinance 1分足で当日の**始値（寄付価格）**を取得（全 RUN 共通）
3. 各 WAIT 注文について約定判定を行う（→ §6 参照）
4. `trades.csv` の WAIT 行を FILLED / UNFILLED に更新
5. `portfolio.json` を更新
6. `daily_summary.csv` に本日の資産状況を記録
7. `pending_orders.json` を削除

---

## 6. 注文方式

### 対象アクション

| アクション | 意味 |
|---|---|
| BUY | ロング建て（現物買い） |
| SELL | ロング決済（現物売り） |
| SHORT | ショート建て（空売り） |
| COVER | ショート決済（買い戻し） |

HOLD は注文を出さない（ログに記録しない）。

### 約定ルール

```
BUY   注文: 成り行き → 始値で FILLED
            始値取得不可（ストップ安気配等）→ UNFILLED

SELL  注文: 寄付価格 ≥ limit_price → FILLED（高く売りたい）
            寄付価格 < limit_price → UNFILLED（即キャンセル）

SHORT 注文: 成り行き → 始値で FILLED
            始値取得不可（ストップ高気配等）→ UNFILLED

COVER 注文: 寄付価格 ≤ limit_price → FILLED（安く買い戻したい）
            寄付価格 > limit_price → UNFILLED（即キャンセル）
```

約定価格は**実際の寄付価格（1分足第1足の始値）**。UNFILLED は即キャンセル。翌営業日に Claude が再判断する。

### その他の UNFILLED 条件

価格条件を満たしていても以下の場合は UNFILLED:

| 条件 | 対象アクション |
|---|---|
| 現金残高が不足 | BUY, COVER |
| 保有株数が不足 | SELL |
| ショート建玉なし | COVER |
| ロング集中度が上限（30%）超過 | BUY |
| ショート建玉合計が上限（250,000 円）超過 | SHORT |

### キャッシュフロー（SHORT / COVER）

| アクション | 現金への影響 |
|---|---|
| SHORT FILLED | `cash += 約定株数 × 約定価格`（空売り代金受取） |
| COVER FILLED | `cash -= 約定株数 × 約定価格`（買い戻し代金支払い） |

### 最終日の処理

終了日には Claude の判断を経ずに全ポジション（ロング・ショート）を**成行**で強制決済する。

---

## 7. Claude の役割

### 受け取る情報（Input）

```
- 現金残高
- ロングポジション（銘柄・株数・取得単価・参考現在価格・含み損益）
- ショートポジション（銘柄・株数・建値・参考現在価格・含み損益）
- 直近 5 営業日の OHLCV × 全対象銘柄
- 直近 10 件の確定済み取引履歴（WAIT を除く）
```

### 返す情報（Output Schema）

```json
[
  { "symbol": "7203", "action": "BUY",   "shares": 100, "limit_price": 2850 },
  { "symbol": "9984", "action": "SHORT", "shares": 100, "limit_price": 9200 },
  { "symbol": "6758", "action": "COVER", "shares": 100, "limit_price": 3500 },
  { "symbol": "8306", "action": "SELL",  "shares": 100, "limit_price": 1720 }
]
```

HOLD は出力しない。判断なしの場合は空配列 `[]`。

### Claude が決定すること

| 判断 | 担当 |
|---|---|
| どの銘柄を取引するか | Claude |
| BUY / SELL / SHORT / COVER / HOLD | Claude |
| 株数 | Claude |
| 指値（limit_price） | Claude |

### Claude が決定しないこと

| 事項 | 担当 |
|---|---|
| 実際の約定価格 | 市場（寄付価格） |
| 約定するかどうか | execute.py（価格比較） |

### エラー時の挙動

Claude の呼び出しが失敗した場合（タイムアウト・パースエラー等）は空配列として扱い、全銘柄 HOLD。ログに記録する。

---

## 8. データスキーマ

### data/runs/{run_id}/trades.csv

```csv
date,time,symbol,action,shares,price,status,cash_after
2026-05-26,08:30,7203,BUY,100,,WAIT,341700
2026-05-26,09:05,7203,BUY,100,2830,FILLED,56700
2026-05-26,08:30,9984,SHORT,100,,WAIT,56700
2026-05-26,09:05,9984,SHORT,100,9180,FILLED,975700
```

| カラム | 説明 |
|---|---|
| date | 取引日（YYYY-MM-DD） |
| time | 08:30 = WAIT 登録、09:05 = 約定処理 |
| symbol | 銘柄コード |
| action | BUY / SELL / SHORT / COVER |
| shares | 株数 |
| price | 約定価格（WAIT 時は空） |
| status | WAIT / FILLED / UNFILLED |
| cash_after | 処理後の現金残高 |

### data/runs/{run_id}/portfolio.json

```json
{
  "cash": 975700,
  "positions": {
    "7203": { "shares": 100, "avg_price": 2830.0 }
  },
  "short_positions": {
    "9984": { "shares": 100, "avg_short_price": 9180.0 }
  },
  "last_updated": "2026-05-26",
  "initial_cash": 500000
}
```

**総資産の計算:**

```
total_value = cash
            + Σ(long_shares  × current_price)
            − Σ(short_shares × current_price)
```

ショートの含み益は `(avg_short_price − current_price) × short_shares`。

### data/runs/{run_id}/daily_summary.csv

```csv
date,cash,long_value,short_exposure,total_value
2026-05-26,975700,283000,918000,340700
```

| カラム | 説明 |
|---|---|
| cash | 現金残高 |
| long_value | ロング株式の時価評価合計 |
| short_exposure | ショート建玉の時価評価合計（負債） |
| total_value | cash + long_value − short_exposure |

execute.py 完了後に 1 日 1 行書き込む。チャートデータのソース。

### data/runs/{run_id}/pending_orders.json

```json
{
  "date": "2026-05-26",
  "orders": [
    { "symbol": "7203", "action": "BUY",   "shares": 100, "limit_price": 2850 },
    { "symbol": "9984", "action": "SHORT",  "shares": 100, "limit_price": 9200 }
  ]
}
```

execute.py 完了後に削除される。

---

## 9. ダッシュボード

- ローカル HTTP サーバー（`python -m http.server 8000`）で配信
- `http://localhost:8000/frontend/` でアクセス
- 60 秒ごとにデータを自動再取得

### RUN の切り替え

画面上部に **セレクタ** を配置し、RUN を切り替える。選択した RUN のデータだけを表示する。

```
[ Run 1 ▼ ]  ← セレクタで切り替え
```

`runs.json` を読み込んでセレクタを動的に生成する。

### 表示内容

- RUN セレクタ
- 資産サマリ（現金・ロング評価・ショート建玉・総資産・損益・損益率）
- 総資産推移グラフ（折れ線）
- 現金残高推移グラフ（折れ線）
- ロングポジション一覧
- ショートポジション一覧
- 取引履歴（WAIT は黄色ハイライト）

