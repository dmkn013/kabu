# Windows タスクスケジューラにタスクを登録する
#   healthcheck.py  (03:00) -> パイプライン健全性の自律調査・修正
#   update_ohlcv.py (17:00) -> 完了後 research.py を連鎖起動（Stage 1 スクリーニング）
#   decide.py        (08:30) -> Stage 2 深掘り + 売買判断 -> WAIT 登録
#   execute.py       (09:05) -> 当日始値で約定処理

$uvPath  = (Get-Command uv -ErrorAction Stop).Source
$workDir = $PSScriptRoot

Write-Host "タスク登録設定:"
Write-Host "  uv:       $uvPath"
Write-Host "  作業Dir:  $workDir"
Write-Host ""

function Register-KabuTask {
    param(
        [string]$TaskName,
        [string]$ScriptFile,
        [string]$Time,
        [string]$Description,
        [int]$ExecutionTimeLimitHours = 1
    )

    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "既存タスク '$TaskName' を削除しました"
    }

    $scriptPath = "$workDir\scripts\$ScriptFile"

    $actionParams = @{
        Execute          = $uvPath
        Argument         = "run python `"$scriptPath`""
        WorkingDirectory = $workDir
    }
    $action = New-ScheduledTaskAction @actionParams

    # Daily トリガー（週末スキップは各スクリプト側で行う）
    # Weekly+複数曜日指定は次回実行日の計算バグがあるため Daily を使用
    $trigger = New-ScheduledTaskTrigger -Daily -At $Time

    $settingsParams = @{
        ExecutionTimeLimit = (New-TimeSpan -Hours $ExecutionTimeLimitHours)
        RestartCount       = 1
        RestartInterval    = (New-TimeSpan -Minutes 5)
        StartWhenAvailable = $true
    }
    $settings = New-ScheduledTaskSettingsSet @settingsParams

    $principalParams = @{
        UserId    = $env:USERNAME
        LogonType = 'Interactive'
        RunLevel  = 'Limited'
    }
    $principal = New-ScheduledTaskPrincipal @principalParams

    $registerParams = @{
        TaskName    = $TaskName
        Action      = $action
        Trigger     = $trigger
        Settings    = $settings
        Principal   = $principal
        Description = $Description
        Force       = $true
    }
    Register-ScheduledTask @registerParams | Out-Null

    Write-Host "登録完了: '$TaskName' ($Time)"
}

# Task 0: 11:00 -- ヘルスチェック（自律調査・修正）
Register-KabuTask -TaskName "KabuSimulation_Healthcheck" -ScriptFile "healthcheck.py" -Time "11:00AM" -Description "日本株シミュレーション ヘルスチェック: 11:00 前日分パイプラインを調査・修正 -> logs/healthcheck_YYYY-MM-DD.txt" -ExecutionTimeLimitHours 2

# Task 0b: 11:35 -- 前場終了スナップショット
Register-KabuTask -TaskName "KabuSimulation_Snapshot_AM" -ScriptFile "snapshot.py" -Time "11:35AM" -Description "日本株シミュレーション スナップショット: 11:35 前場終了後の現在値を取得 -> intraday.csv"

# Task 0c: 15:35 -- 後場終了スナップショット
Register-KabuTask -TaskName "KabuSimulation_Snapshot_PM" -ScriptFile "snapshot.py" -Time "03:35PM" -Description "日本株シミュレーション スナップショット: 15:35 後場終了後の現在値を取得 -> intraday.csv"

# Task 1: 17:00 -- OHLCV 更新 + research.py 連鎖（Stage 1 スクリーニング）
#   research はレート制限待機を含むため実行時間上限を長め（12時間）に設定
Register-KabuTask -TaskName "KabuSimulation_Research" -ScriptFile "update_ohlcv.py" -Time "05:00PM" -Description "日本株シミュレーション Stage1: 17:00 OHLCV更新 -> research.py で候補銘柄スクリーニング -> shortlist.json" -ExecutionTimeLimitHours 12

# Task 2: 8:30 -- 売買判断（Stage 2: 候補の深掘り + Claude の意思決定）
Register-KabuTask -TaskName "KabuSimulation_Decide" -ScriptFile "decide.py" -Time "08:30AM" -Description "日本株シミュレーション Stage2: 8:30 候補銘柄を深掘りして売買判断 -> WAIT 登録" -ExecutionTimeLimitHours 2

# Task 3: 9:15 -- 約定処理（当日始値で執行）
# 09:00 市場オープンから15分バッファ。yfinance fast_info キャッシュが追いつくのを待つ
Register-KabuTask -TaskName "KabuSimulation_Execute" -ScriptFile "execute.py" -Time "09:15AM" -Description "日本株シミュレーション Step3: 9:15 当日始値で約定処理 -> FILLED/UNFILLED 更新"

Write-Host ""
Write-Host "登録済みタスク確認:"
Get-ScheduledTask | Where-Object { $_.TaskName -like "KabuSimulation*" } | ForEach-Object {
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
    [PSCustomObject]@{ TaskName = $_.TaskName; NextRun = $info.NextRunTime; State = $_.State }
} | Format-Table -AutoSize

Write-Host ""
Write-Host "手動テスト:"
Write-Host "  Start-ScheduledTask -TaskName 'KabuSimulation_Healthcheck'"
Write-Host "  Start-ScheduledTask -TaskName 'KabuSimulation_Snapshot_AM'"
Write-Host "  Start-ScheduledTask -TaskName 'KabuSimulation_Snapshot_PM'"
Write-Host "  Start-ScheduledTask -TaskName 'KabuSimulation_Research'"
Write-Host "  Start-ScheduledTask -TaskName 'KabuSimulation_Decide'"
Write-Host "  Start-ScheduledTask -TaskName 'KabuSimulation_Execute'"
Write-Host ""
Write-Host "タスク削除:"
Write-Host "  'KabuSimulation_Healthcheck','KabuSimulation_Snapshot_AM','KabuSimulation_Snapshot_PM','KabuSimulation_Research','KabuSimulation_Decide','KabuSimulation_Execute' | ForEach-Object { Unregister-ScheduledTask -TaskName `$_ -Confirm:`$false }"
