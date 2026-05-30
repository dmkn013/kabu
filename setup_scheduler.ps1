# Windows タスクスケジューラに2つのタスクを登録する
# decide.py (8:30) と execute.py (9:05) を毎営業日に実行

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
        [string]$Description
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

    $triggerParams = @{
        Weekly     = $true
        DaysOfWeek = @('Monday','Tuesday','Wednesday','Thursday','Friday')
        At         = $Time
    }
    $trigger = New-ScheduledTaskTrigger @triggerParams

    $settingsParams = @{
        ExecutionTimeLimit = (New-TimeSpan -Hours 1)
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

# Task 1: 8:30 -- 売買判断（Claude による意思決定）
Register-KabuTask -TaskName "KabuSimulation_Decide" -ScriptFile "decide.py" -Time "08:30AM" -Description "日本株シミュレーション Step1: 8:30 Claude による売買判断 -> WAIT 登録"

# Task 2: 9:05 -- 約定処理（当日始値で執行）
Register-KabuTask -TaskName "KabuSimulation_Execute" -ScriptFile "execute.py" -Time "09:05AM" -Description "日本株シミュレーション Step2: 9:05 当日始値(1分足)で約定処理 -> FILLED/UNFILLED 更新"

Write-Host ""
Write-Host "登録済みタスク確認:"
Get-ScheduledTask | Where-Object { $_.TaskName -like "KabuSimulation*" } | ForEach-Object {
    $info = Get-ScheduledTaskInfo -TaskName $_.TaskName
    [PSCustomObject]@{ TaskName = $_.TaskName; NextRun = $info.NextRunTime; State = $_.State }
} | Format-Table -AutoSize

Write-Host ""
Write-Host "手動テスト:"
Write-Host "  Start-ScheduledTask -TaskName 'KabuSimulation_Decide'"
Write-Host "  Start-ScheduledTask -TaskName 'KabuSimulation_Execute'"
Write-Host ""
Write-Host "タスク削除:"
Write-Host "  'KabuSimulation_Decide','KabuSimulation_Execute' | ForEach-Object { Unregister-ScheduledTask -TaskName `$_ -Confirm:`$false }"
