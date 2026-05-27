# ==============================================================
# Blofin Auto Trader – 10x LEVERAGE / 30% PROFIT RUNNER
# ==============================================================
# This script launches the bot in "10x30" mode:
#   - Uses 10x leverage
#   - Targets 3% asset move = 30% return on margin
#   - Compounds profits indefinitely
#   - Auto-restarts on crash
# ==============================================================

param(
    [switch]$Background,
    [switch]$NoRestart,
    [switch]$DryRun,
    [int]$MaxRestarts = 99999,
    [int]$RetryDelay = 15
)

$BotDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BotPy = Join-Path $BotDir "bot.py"
$EnvFile = Join-Path $BotDir ".env"
$BackupEnv = Join-Path $BotDir ".env.backup"
$Env10x30Example = Join-Path $BotDir ".env.10x30.example"
$LogDir = Join-Path $BotDir "logs"
$RunnerLog = Join-Path $LogDir "runner_10x30.log"

# Ensure log directory exists
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Colors for console output
$Green = "Green"
$Yellow = "Yellow"
$Red = "Red"
$Cyan = "Cyan"

function Write-Log {
    param([string]$Message, [string]$Color = "White")
    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$Timestamp $Message" | Out-File -FilePath $RunnerLog -Encoding utf8 -Append
    Write-Host "$Timestamp $Message" -ForegroundColor $Color
}

function Test-Env10x30 {
    <#
    .SYNOPSIS
    Validates that the .env file has the required 10x30 settings.
    #>
    if (-not (Test-Path $EnvFile)) {
        Write-Log "ERROR: .env file not found at $EnvFile" -Color $Red
        return $false
    }
    
    $envContent = Get-Content $EnvFile -Raw
    $hasApiKey = $envContent -match "BLOFIN_API_KEY=.+"
    $hasSecret = $envContent -match "BLOFIN_SECRET=.+"
    $hasPassphrase = $envContent -match "BLOFIN_PASSPHRASE=.+"
    $hasMode10x30 = $envContent -match "SIGNAL_MODE=10x30"
    $hasLeverage10 = $envContent -match "LEVERAGE=10"
    
    if (-not $hasMode10x30) {
        Write-Log "WARNING: SIGNAL_MODE is not '10x30' in .env - strategy may not work!" -Color $Yellow
    }
    if (-not $hasLeverage10) {
        Write-Log "WARNING: LEVERAGE is not 10 in .env" -Color $Yellow
    }
    if (-not $hasApiKey -or -not $hasSecret -or -not $hasPassphrase) {
        Write-Log "ERROR: Missing API credentials in .env" -Color $Red
        return $false
    }
    
    return $true
}

function Show-StrategyHeader {
    Write-Log "================================" -Color $Cyan
    Write-Log " BLOFIN 10x30 AUTO TRADER" -Color $Cyan
    Write-Log "================================" -Color $Cyan
    Write-Log " Leverage: 10x" -Color $Cyan
    Write-Log " Target:   3% asset move = 30% on margin" -Color $Cyan
    Write-Log " Stop:     1.2% asset move = 12% max loss" -Color $Cyan
    Write-Log " RR Ratio: 2.5 : 1" -Color $Cyan
    Write-Log " Compounding: YES (reinvest all profits)" -Color $Cyan
    Write-Log "================================" -Color $Cyan
}

# === MAIN ===
Write-Log "=== Blofin 10x30 Auto Trader Starting ===" -Color $Green

if (-not (Test-Path $EnvFile)) {
    Write-Log "ERROR: No .env file found." -Color $Red
    Write-Log "Copy .env.10x30.example to .env and fill in your API keys:" -Color $Yellow
    Write-Log "  Copy-Item '$Env10x30Example' '$EnvFile'" -Color $Yellow
    exit 1
}

if (-not (Test-Env10x30)) {
    Write-Log "WARNING: .env validation had warnings. Review your settings." -Color $Yellow
    Start-Sleep -Seconds 3
}

Show-StrategyHeader

if (-not $DryRun) {
    # Check if DRY_RUN mode is active
    $envContent = Get-Content $EnvFile -Raw
    if ($envContent -match "DRY_RUN=true") {
        Write-Log "WARNING: DRY_RUN is true! Set DRY_RUN=false in .env to trade live." -Color $Yellow
    } else {
        Write-Log "Live trading mode - executing real orders!" -Color $Red
        Write-Log "Press Ctrl+C within 10 seconds to abort..." -Color $Red
        Start-Sleep -Seconds 10
    }
} else {
    Write-Log "DRY RUN: Simulating trades (no real orders will be placed)" -Color $Yellow
    # Temporarily set DRY_RUN=true for test run
    $env:DRY_RUN_OVERRIDE = "true"
}

Write-Log "Bot dir: $BotDir"
Write-Log "Python: $(py --version)"

$restartCount = 0
$lastRestartTime = Get-Date

while ($restartCount -lt $MaxRestarts) {
    $now = Get-Date
    $uptimeSeconds = [math]::Round(($now - $lastRestartTime).TotalSeconds)
    
    Write-Log "Starting 10x30 bot instance #$($restartCount + 1)..." -Color $Green
    
    if ($Background) {
        # Run in background (detached process)
        $job = Start-Job -ScriptBlock {
            param($path, $logDir)
            $env:PYTHONUNBUFFERED = "1"
            if ($env:DRY_RUN_OVERRIDE -eq "true") {
                $env:DRY_RUN = "true"
            }
            py $path 2>&1 | ForEach-Object {
                "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $_" | Out-File -FilePath (Join-Path $logDir "bot_10x30_output.log") -Encoding utf8 -Append
            }
        } -ArgumentList $BotPy, $LogDir
        
        Write-Log "Bot running in background (Job ID: $($job.Id))"
        
        if (-not $NoRestart) {
            # Wait for job to complete or fail
            Wait-Job $job -Timeout 86400  # Max 24 hours per run
            if ($job.State -eq "Running") {
                Write-Log "Bot still running after 24h check – healthy" -Color $Green
                continue
            }
            Receive-Job $job | Out-Null
            Remove-Job $job -ErrorAction SilentlyContinue
        }
    } else {
        # Run in foreground
        Write-Log "Bot running in foreground. Press Ctrl+C to stop." -Color $Cyan
        try {
            $env:PYTHONUNBUFFERED = "1"
            py $BotPy
            $exitCode = $LASTEXITCODE
            Write-Log "Bot exited with code: $exitCode" -Color $Yellow
        }
        catch {
            Write-Log "Bot crashed with exception: $_" -Color $Red
        }
    }
    
    if ($NoRestart) {
        Write-Log "NoRestart flag set – exiting." -Color $Yellow
        break
    }
    
    $restartCount++
    $lastRestartTime = Get-Date
    
    Write-Log "Restarting in ${RetryDelay}s... (restart #$restartCount)" -Color $Yellow
    Start-Sleep -Seconds $RetryDelay
}

Write-Log "=== 10x30 Runner stopping (restarts exhausted or stopped) ===" -Color $Green