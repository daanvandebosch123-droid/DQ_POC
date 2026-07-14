# Starts DQTool as a central server for the team.
# Usage: .\start-server.ps1            (HTTP)
#        .\start-server.ps1 -Https     (HTTPS, expects the cert files below)

param(
    [switch]$Https,
    [string]$CertFile = "C:\certs\dqtool.crt",
    [string]$KeyFile = "C:\certs\dqtool.key",
    [string]$BindHost = "0.0.0.0",
    [int]$Port = 8080
)

$env:DQTOOL_HOST = $BindHost
$env:DQTOOL_PORT = "$Port"

if ($Https) {
    if (-not (Test-Path $CertFile) -or -not (Test-Path $KeyFile)) {
        Write-Error "Certificate files not found: $CertFile / $KeyFile"
        exit 1
    }
    $env:DQTOOL_SSL_CERTFILE = $CertFile
    $env:DQTOOL_SSL_KEYFILE = $KeyFile
    Write-Host "Starting DQTool on https://$($env:COMPUTERNAME):$Port"
} else {
    Remove-Item Env:DQTOOL_SSL_CERTFILE -ErrorAction SilentlyContinue
    Remove-Item Env:DQTOOL_SSL_KEYFILE -ErrorAction SilentlyContinue
    Write-Host "Starting DQTool on http://$($env:COMPUTERNAME):$Port (no TLS)"
}

& "$PSScriptRoot\.venv\Scripts\python.exe" -m dqtool.app
