# Blofin Auto Trader – 20-year production runner
# This script runs the bot continuously with auto-restart, crash recovery,
# and optional Windows Task Scheduler integration.

param(
    [switch]$Background,
    [switch]$NoRestart,
    [int]$MaxRestarts = 99999,
    [int]$RetryDelay = 30
)

$BotDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BotPy = Join-Path $BotDir "bot.py"
$LogDir = Join-Path $BotDir "logs"
$RunnerLog = Join-Path $LogDir "runner.log"

# Ensure log directory exists
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Log {
    param([string]$Message)
    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$Timestamp $Message" | Out-File -FilePath $RunnerLog -Encoding utf8 -Append
    Write-Host "$Timestamp $Message"
}

Write-Log "=== Blofin Auto Trader Runner Starting ==="
Write-Log "Bot dir: $BotDir"
Write-Log "Python: $(py --version)"

$restartCount = 0
$lastRestartTime = Get-Date

while ($restartCount -lt $MaxRestarts) {
    $now = Get-Date
    $uptimeSeconds = [math]::Round(($now - $lastRestartTime).TotalSeconds)
    
    Write-Log "Starting bot instance #$($restartCount + 1)..."
    
    if ($Background) {
        # Run in background (detached process)
        $job = Start-Job -ScriptBlock {
            param($path, $logDir)
            $env:PYTHONUNBUFFERED = "1"
            py $path 2>&1 | ForEach-Object {
                "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $_" | Out-File -FilePath (Join-Path $logDir "bot_output.log") -Encoding utf8 -Append
            }
        } -ArgumentList $BotPy, $LogDir
        
        Write-Log "Bot running in background (Job ID: $($job.Id))"
        
        if (-not $NoRestart) {
            # Wait for job to complete or fail
            Wait-Job $job -Timeout 86400  # Max 24 hours per run
            if ($job.State -eq "Running") {
                Write-Log "Bot still running after 24h check – healthy"
                continue
            }
            Receive-Job $job | Out-Null
            Remove-Job $job -ErrorAction SilentlyContinue
        }
    } else {
        # Run in foreground
        Write-Log "Bot running in foreground. Press Ctrl+C to stop."
        try {
            $env:PYTHONUNBUFFERED = "1"
            py $BotPy
            $exitCode = $LASTEXITCODE
            Write-Log "Bot exited with code: $exitCode"
        }
        catch {
            Write-Log "Bot crashed with exception: $_"
        }
    }
    
    if ($NoRestart) {
        Write-Log "NoRestart flag set – exiting."
        break
    }
    
    $restartCount++
    $lastRestartTime = Get-Date
    
    Write-Log "Restarting in ${RetryDelay}s... (restart #$restartCount)"
    Start-Sleep -Seconds $RetryDelay
}

Write-Log "=== Runner stopping (restarts exhausted or stopped) ==="