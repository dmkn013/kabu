#!/usr/bin/env python3
"""
毎日 03:00（平日）に起動する自律ヘルスチェックランチャー。
Claude Code に過去24時間のパイプライン調査・修正を依頼する。

結果は logs/healthcheck_YYYY-MM-DD.txt に記録し、
[healthcheck] YYYY-MM-DD: OK/FIXED/ERROR でコミット・プッシュする。
"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import logging
import os
import subprocess
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
os.chdir(REPO_ROOT)

LOG_DIR = REPO_ROOT / 'logs'
LOG_DIR.mkdir(exist_ok=True)

today_str = date.today().strftime('%Y-%m-%d')
log_path = LOG_DIR / f'healthcheck_{today_str}.txt'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(log_path), encoding='utf-8'),
    ],
)
logger = logging.getLogger(__name__)

PROMPT = f"""あなたは日本株売買シミュレーションシステムの自律ヘルスチェックエージェントです。
今日の日付は {today_str} です。作業ディレクトリは C:\\Users\\shun\\work\\kabu です。

## 目的
過去24時間のパイプラインが specification.md の仕様通りに動作したか検査し、
問題があれば修正して、結果を記録・コミットする。

## 手順

### Step 1: 仕様を把握する
`specification.md` を読み込んでシステム全体の仕様・期待動作を把握する。

### Step 2: パイプラインの健全性を検査する

以下の順序でチェックし、各項目を PASS / FAIL で記録する。

**A. タスクスケジューラの実行結果**
PowerShell で以下を実行:
```
Get-ScheduledTask | Where-Object {{ $_.TaskName -like "KabuSimulation*" }} | ForEach-Object {{
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
    [PSCustomObject]@{{ Task=$_.TaskName; LastRun=$info.LastRunTime; Result=$info.LastTaskResult }}
}} | Format-Table -AutoSize
```
- Decide / Execute / Research / Healthcheck の LastTaskResult が 0 か確認
- 0 以外 = 失敗

**B. git コミットの存在**
```
git log --oneline --since="{today_str} 00:00" --until="{today_str} 23:59"
```
- `[decide] {today_str}` が存在するか
- `[execute] {today_str}` が存在するか
- `[healthcheck] {today_str}` はまだなくてOK（これから作る）

**C. データファイルの鮮度**
- `data/runs/*/daily_summary.csv` の最終行の date が {today_str} か
- `data/runs/*/portfolio.json` の last_updated が {today_str} か

**D. shortlist.json の内容**
- `data/shortlist.json` が存在するか
- date が翌営業日付（土日をスキップした明日以降）か
- count が 1 以上か

**E. GitHub Actions のデプロイ状況**
```
gh run list --limit 5 --repo dmkn013/kabu
```
- 直近の run が `completed / success` か

### Step 3: 問題を修正する

FAIL があれば原因を特定して修正する。修正の権限は無制限（コード編集・スクリプト再実行・タスクスケジューラ変更・git操作すべて許可）。

よくある問題と対処:
- スケジューラ終了コード非0 → ログを調べてエラー原因を特定・コード修正・必要なら再実行
- gitコミットがない → スクリプトを手動で再実行（`uv run python scripts/xxx.py --force`）
- shortlist.jsonが古い・ない → `uv run python scripts/research.py` を再実行
- GitHub Actions失敗 → `git push` を再試行

### Step 4: ログを書き出す

`logs/healthcheck_{today_str}.txt` に以下の形式で追記する（既存内容の後ろに append）:

```
=== Healthcheck {today_str} ===
[PASS/FAIL] A. スケジューラ: Decide=<結果>, Execute=<結果>, Research=<結果>
[PASS/FAIL] B. gitコミット: decide=<あり/なし>, execute=<あり/なし>
[PASS/FAIL] C. データ鮮度: daily_summary=<日付>, portfolio=<日付>
[PASS/FAIL] D. shortlist.json: date=<日付>, count=<件数>
[PASS/FAIL] E. GitHub Actions: <status>

修正内容:
- <修正した内容を箇条書き、なければ「なし」>

総合判定: OK / FIXED / ERROR
```

### Step 5: git コミット・プッシュ

```
git add logs/healthcheck_{today_str}.txt
git add data/  # 修正によってデータが変わった場合
git add scripts/  # コードを修正した場合
git commit -m "[healthcheck] {today_str}: <OK または FIXED: 概要>"
git push
```

問題なければ `OK`、修正した場合は `FIXED: <何を修正したか>` をコミットメッセージに含める。
エラーが修正できなかった場合は `ERROR: <概要>` とし、それでもコミット・プッシュする。
"""


def main() -> int:
    logger.info(f'[healthcheck] {today_str} 開始')

    # PATH に Claude CLI のディレクトリを追加
    env = os.environ.copy()
    try:
        result = subprocess.run(
            ['powershell', '-NonInteractive', '-Command',
             "[System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + "
             "[System.Environment]::GetEnvironmentVariable('Path','User')"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            env['PATH'] = result.stdout.strip()
    except Exception:
        pass

    cmd = ['claude', '--dangerously-skip-permissions', '-p', '--model', 'claude-sonnet-4-6']

    try:
        result = subprocess.run(
            cmd,
            input=PROMPT,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=7200,  # 2時間
            env=env,
            cwd=str(REPO_ROOT),
        )
        if result.stdout:
            logger.info(result.stdout)
        if result.stderr:
            logger.warning(f'stderr: {result.stderr[:500]}')
        logger.info(f'[healthcheck] Claude 終了 (exit={result.returncode})')
        return result.returncode
    except subprocess.TimeoutExpired:
        logger.error('[healthcheck] タイムアウト（2時間超過）')
        return 1
    except FileNotFoundError:
        logger.error('[healthcheck] claude コマンドが見つかりません')
        return 1


if __name__ == '__main__':
    sys.exit(main())
