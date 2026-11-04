$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$playerPath = Join-Path $projectRoot 'engine\Build\RoboHalo.exe'
$commands = @(
    'Bring the red sphere',
    'Bring the red cube',
    'Bring the blue sphere',
    'Bring the blue cube',
    'Bring the yellow cylinder',
    'Bring the yellow cone'
)

foreach ($command in $commands) {
    $player = Start-Process -FilePath $playerPath -ArgumentList @('-batchmode', '-nographics', '-logFile', (Join-Path $projectRoot 'player-manual-acceptance.log')) -WindowStyle Hidden -PassThru
    try {
        $ready = $false
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            try {
                $null = Invoke-RestMethod 'http://localhost:8080/state' -TimeoutSec 1
                $ready = $true
                break
            } catch {
                Start-Sleep -Milliseconds 500
            }
        }
        if (-not $ready) { throw 'Engine HTTP server did not become ready' }
        $output = python -m agent.manual_agent $command | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0 -or $output.outcome -ne 'pass') {
            throw "Manual acceptance failed for '$command'"
        }
        Write-Output "PASS: $command ($($output.step_count) steps, $($output.retries) retries)"
    } finally {
        if (-not $player.HasExited) { Stop-Process -Id $player.Id }
        $player.WaitForExit(5000) | Out-Null
    }
}
