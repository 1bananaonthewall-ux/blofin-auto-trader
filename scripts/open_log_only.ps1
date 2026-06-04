$Root = 'C:\Users\mknig\blofin-auto-trader'
Set-Location $Root
$Host.UI.RawUI.WindowTitle = 'God Bot Live Log'

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class LogWin {
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@
$hwnd = (Get-Process -Id $PID).MainWindowHandle
if ($hwnd -ne [IntPtr]::Zero) {
    [void][LogWin]::ShowWindow($hwnd, 3)   # SW_MAXIMIZE
    [void][LogWin]::SetForegroundWindow($hwnd)
}

Write-Host 'God Bot live log (Ctrl+C stops tail only)' -ForegroundColor Cyan
Get-Content (Join-Path $Root 'logs\bot.log') -Wait -Tail 150
