$ErrorActionPreference = 'Stop'
$dashboardUrl = 'http://127.0.0.1:8080/'
$sshTarget = 'jzewdu@Pi3-Earth.local'
$sshForward = '8080:127.0.0.1:8080'

function Get-DashboardStatusCode {
    try {
        $request = [System.Net.HttpWebRequest]::Create($dashboardUrl)
        $request.Method = 'GET'
        $request.Timeout = 1500
        $response = $request.GetResponse()
        try { return [int]$response.StatusCode } finally { $response.Close() }
    }
    catch [System.Net.WebException] {
        if ($_.Exception.Response) {
            $code = [int]$_.Exception.Response.StatusCode
            $_.Exception.Response.Close()
            return $code
        }
        return 0
    }
}

$currentStatus = Get-DashboardStatusCode
if ($currentStatus -eq 200 -or $currentStatus -eq 401) {
    Write-Host 'Ares is already reachable through an SSH tunnel.' -ForegroundColor Green
    Start-Process $dashboardUrl
    exit 0
}

$listener = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw 'Port 8080 is being used by another app. Close that app, then open this launcher again.'
}

$ssh = Get-Command ssh.exe -ErrorAction Stop
Write-Host 'Opening a private connection to Pi3-Earth...' -ForegroundColor Cyan
Write-Host 'Enter the Pi login password in the SSH window. The Ares dashboard will open when connected.'
$tunnel = Start-Process -FilePath $ssh.Source -ArgumentList "-N -L 127.0.0.1:$sshForward $sshTarget" -PassThru -WindowStyle Normal

$deadline = (Get-Date).AddSeconds(90)
$statusCode = 0
do {
    if ($tunnel.HasExited) {
        throw 'The Pi connection closed before the dashboard became available. Check that Pi3-Earth is on and connected to Wi-Fi.'
    }
    $statusCode = Get-DashboardStatusCode
    if ($statusCode -eq 200 -or $statusCode -eq 401) { break }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)

if ($statusCode -ne 200 -and $statusCode -ne 401) {
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
    throw 'Could not reach the Ares dashboard on the Pi. Check the Pi and try again.'
}

Write-Host 'Secure connection ready. Sign in to the dashboard in your browser.' -ForegroundColor Green
Write-Host 'Keep the SSH window open while using Ares. Close it when you are done.'
Start-Process $dashboardUrl
Wait-Process -Id $tunnel.Id
Write-Host 'Ares connection closed.'
Read-Host 'Press Enter to close this window'
