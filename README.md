
# 日本株シミュレーション
Claude CLI を意思決定エンジンとして使い、日本株の売買をシミュレーションするアプリ。

- 初期資金 500,000 円から出発し、市場 30 日分が経過した時点の総資産を最大化するのが目標
- 毎営業日、Claude が前日までの OHLCV を見て売買判断を行う
- 複数の独立した RUN を並行させて戦略を比較できる

---

## セットアップ

```powershell
# 依存パッケージのインストール
uv pip install -r scripts/requirements.txt
```

Claude Code CLI（`claude` コマンド）が使えることを確認しておく。

---

## 使い方

### 1. RUN を作成する

```powershell
uv run python scripts/new_run.py --name "Run 1"
```

`data/runs/run_001/` が作られ、`data/runs.json` に登録される。

### 2. ダッシュボードを起動する

```powershell
uv run python -m http.server 8000
```

ブラウザで `http://localhost:8000/frontend/` を開く。60 秒ごとに自動更新される。

### 3. タスクスケジューラを登録する（初回のみ）

```powershell
.\setup_scheduler.ps1
```

以後、毎営業日 17:00（Research）→ 8:30（Decide）→ 9:00〜15:30（Snapshot）→ 9:15（Execute）→ 11:00（Healthcheck）に自動実行される。

### 手動実行（テスト用）

```powershell
uv run python scripts/update_ohlcv.py  # 17:00 相当: OHLCV更新 -> research.py 連鎖
uv run python scripts/decide.py        # 8:30 相当: Claude が注文を決定
uv run python scripts/execute.py       # 9:15 相当: 始値で約定判定
uv run python scripts/snapshot.py      # 09:00〜15:30 相当: 日中スナップショット
uv run python scripts/healthcheck.py   # 11:00 相当: 前日分パイプラインの調査・修正
```

### ターミナルで状況確認

```powershell
uv run python scripts/show_status.py              # 全 RUN 一覧
uv run python scripts/show_status.py --run run_001  # 特定 RUN の詳細
```

---

## 1 日の流れ

```
17:00 update_ohlcv.py → research.py（Stage 1 スクリーニング）
  └─ yfinance で OHLCV を更新
  └─ プライム全銘柄をグループ分割し、Claude が有望銘柄を選抜
  └─ data/shortlist.json に翌営業日分の候補を出力

8:30  decide.py（Stage 2 深掘り + 売買判断）
  └─ shortlist.json の候補を深掘りし、各 RUN に対して独立して Claude を呼び出す
  └─ 注文を pending_orders.json に保存
  └─ trades.csv に WAIT ステータスで追記
  └─ ダッシュボードに当日の WAIT 注文が表示される

9:00〜15:30 snapshot.py（10 分おき）
  └─ 保有ポジションの現在値を取得
  └─ intraday.csv に追記、market_prices.json を更新

9:15  execute.py
  └─ yfinance で当日の始値（寄付価格）を取得
  └─ 各 WAIT 注文を寄付き指値ルールで約定判定
  └─ trades.csv の WAIT 行を FILLED / UNFILLED に更新
  └─ portfolio.json を更新
  └─ daily_summary.csv に本日の資産状況を追記
  └─ pending_orders.json を削除

11:00 healthcheck.py
  └─ 前日分のパイプライン実行結果を自律調査
  └─ 異常があれば修正し logs/healthcheck_YYYY-MM-DD.txt に記録
```

---

## 注文方式（寄付き指値）

Claude が `limit_price` を指定し、当日の始値と比較して約定を判定する。約定価格は指値ではなく実際の始値。

| アクション | 約定条件 | 意味 |
|---|---|---|
| BUY | 始値 ≤ limit_price | 安く買いたい |
| SELL | 始値 ≥ limit_price | 高く売りたい |
| SHORT | 始値 ≥ limit_price | 高く空売りしたい |
| COVER | 始値 ≤ limit_price | 安く買い戻したい |

条件を満たさない場合は即キャンセル（UNFILLED）。翌営業日に Claude が再判断する。

---

## ファイル構成

```
kabu/
├── config.json                      # 初期資金・リスク上限などの設定
├── setup_scheduler.ps1              # タスクスケジューラ登録スクリプト
├── specification.md                 # 仕様書（実装の基準）
│
├── scripts/
│   ├── decide.py               # 8:30 実行: Stage2 深掘り + Claude が売買判断 → WAIT 登録
│   ├── execute.py              # 9:15 実行: 始値で約定判定 → FILLED/UNFILLED
│   ├── snapshot.py             # 9:00〜15:30 10分おき実行: 日中スナップショット → intraday.csv
│   ├── healthcheck.py          # 11:00 実行: 前日分パイプラインの自律調査・修正
│   ├── update_ohlcv.py         # 17:00 実行: OHLCV更新 → research.py を連鎖起動
│   ├── research.py             # Stage1: プライム全銘柄からトーナメント方式で候補選抜 → shortlist.json
│   ├── fetch_data.py           # yfinance で東証 OHLCV 取得
│   ├── fetch_topix.py          # TOPIX/プライム全銘柄マスタの取得
│   ├── portfolio.py            # Portfolio クラス（BUY/SELL/SHORT/COVER）
│   ├── claude_agent.py         # claude -p サブプロセス呼び出し
│   ├── new_run.py              # 新しい RUN を作成するユーティリティ
│   ├── init_ohlcv.py           # OHLCV キャッシュの初回一括取得ユーティリティ
│   ├── backfill_intraday_range.py  # intraday.csv を任意期間で遡及生成するユーティリティ
│   ├── show_status.py          # ターミナルで状況確認
│   └── requirements.txt
│
├── data/
│   ├── runs.json        # 全 RUN のインデックス
│   ├── shortlist.json   # Stage1 で選抜された翌営業日分の候補銘柄
│   ├── ohlcv/            # 銘柄ごとの日次 OHLCV キャッシュ
│   └── runs/
│       └── {run_id}/
│           ├── portfolio.json       # ポートフォリオ状態
│           ├── trades.csv           # 取引ログ
│           ├── daily_summary.csv    # 日次サマリ（チャート用）
│           ├── intraday.csv         # 日中スナップショット（10分おき）
│           └── pending_orders.json  # 当日の WAIT 注文（execute 後に削除）
│
└── frontend/
    ├── index.html
    ├── style.css
    └── script.js
```

---

## config.json

```json
{
  "initial_cash": 500000,
  "max_long_position_pct": 0.30,             // 単一銘柄のロング集中上限（総資産比）
  "max_short_exposure": 250000               // ショート建玉の合計上限（円）
}
```

対象銘柄は固定リストではなく、`research.py` が毎日プライム市場全銘柄から Claude によるトーナメント方式スクリーニングで動的に選抜する。

---

## ダッシュボード

- 画面上部のセレクタで RUN を切り替え
- 現金・ロング評価・ショート建玉・総資産・損益・損益率をカード表示
- 総資産推移・現金残高推移の折れ線グラフ（Chart.js）
- ロングポジション一覧・ショートポジション一覧
- 取引履歴（WAIT 行は黄色ハイライト）

---

## 制約パラメータ

| パラメータ | 値 |
|---|---|
| 初期資金 | 500,000 円 |
| シミュレーション期間 | RUN ごとに設定（例: 30〜40 市場日） |
| 対象銘柄 | プライム市場全銘柄から Claude が動的スクリーニング |
| ロング集中上限 | 総資産の 30%（単一銘柄） |
| ショート建玉上限 | 合計 250,000 円 |
