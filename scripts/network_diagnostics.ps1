$ErrorActionPreference = "Continue"

Write-Host "============================================"
Write-Host "Telegram Network Diagnostics"
Write-Host "============================================"
Write-Host "Timestamp UTC: $([DateTime]::UtcNow.ToString('o'))"
Write-Host "Computer: $env:COMPUTERNAME"
Write-Host "OS: $((Get-CimInstance Win32_OperatingSystem).Caption)"
Write-Host "Runner OS: $env:ImageOS"
Write-Host "Runner Image: $env:ImageVersion"

Write-Host ""
Write-Host "--- Public IPv4 ---"
try {
    $publicIp = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 10).ToString().Trim()
    Write-Host "Public IPv4: $publicIp"
} catch {
    Write-Host "Public IPv4 lookup failed: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "--- DNS ---"
foreach ($name in @("telegram.org", "api.telegram.org")) {
    try {
        $records = Resolve-DnsName -Name $name -Type A -ErrorAction Stop |
            Where-Object { $_.IPAddress } |
            Select-Object -ExpandProperty IPAddress
        Write-Host "$name -> $($records -join ', ')"
    } catch {
        Write-Host "$name DNS lookup failed: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "--- Default route / interface ---"
try {
    Get-NetRoute -DestinationPrefix "0.0.0.0/0" |
        Sort-Object RouteMetric |
        Format-Table ifIndex, InterfaceAlias, NextHop, RouteMetric -AutoSize | Out-String | Write-Host
} catch {
    Write-Host "Default route lookup failed: $($_.Exception.Message)"
}

try {
    Get-NetIPConfiguration |
        Where-Object { $_.IPv4DefaultGateway } |
        Format-List InterfaceAlias, InterfaceIndex, IPv4Address, IPv4DefaultGateway, DNSServer | Out-String | Write-Host
} catch {
    Write-Host "IP configuration lookup failed: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "--- TCP 443 latency to Telegram DC endpoints ---"
$telegramEndpoints = @(
    @{ Dc = "DC1"; Ip = "149.154.175.50" },
    @{ Dc = "DC2"; Ip = "149.154.167.40" },
    @{ Dc = "DC3"; Ip = "149.154.175.100" },
    @{ Dc = "DC4"; Ip = "149.154.167.91" },
    @{ Dc = "DC5"; Ip = "91.108.56.130" }
)

foreach ($endpoint in $telegramEndpoints) {
    try {
        $result = Test-NetConnection -ComputerName $endpoint.Ip -Port 443 -InformationLevel Detailed -WarningAction SilentlyContinue
        Write-Host ("{0} {1}: TcpTestSucceeded={2} RemoteAddress={3} SourceAddress={4} PingSucceeded={5} PingMs={6}" -f `
            $endpoint.Dc, $endpoint.Ip, $result.TcpTestSucceeded, $result.RemoteAddress, $result.SourceAddress, $result.PingSucceeded, $result.PingReplyDetails.RoundtripTime)
    } catch {
        Write-Host "$($endpoint.Dc) $($endpoint.Ip): TCP test failed: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "--- TCP latency repeated test: Telegram DC4 ---"
$dc4 = "149.154.167.91"
for ($i = 1; $i -le 5; $i++) {
    try {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $client = [System.Net.Sockets.TcpClient]::new()
        $task = $client.ConnectAsync($dc4, 443)
        if (-not $task.Wait(10000)) {
            Write-Host "DC4 attempt ${i}: TIMEOUT"
            $client.Dispose()
            continue
        }
        $sw.Stop()
        Write-Host ("DC4 attempt {0}: connect_ms={1:N1}" -f $i, $sw.Elapsed.TotalMilliseconds)
        $client.Dispose()
    } catch {
        Write-Host "DC4 attempt ${i}: FAILED: $($_.Exception.Message)"
    }
}

Write-Host ""
Write-Host "--- Route to Telegram DC4 ---"
try {
    Write-Host "tracert -d $dc4"
    tracert.exe -d -h 12 -w 1000 $dc4
} catch {
    Write-Host "Traceroute failed: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "--- Network adapters ---"
try {
    Get-NetAdapter |
        Where-Object { $_.Status -eq "Up" } |
        Format-Table Name, InterfaceDescription, LinkSpeed, MacAddress, Status -AutoSize | Out-String | Write-Host
} catch {
    Write-Host "Adapter lookup failed: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "--- Generic HTTPS control test ---"
try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $response = Invoke-WebRequest -Uri "https://www.github.com/" -UseBasicParsing -TimeoutSec 15
    $sw.Stop()
    Write-Host "GitHub HTTPS: status=$($response.StatusCode) elapsed_ms=$([math]::Round($sw.Elapsed.TotalMilliseconds,1))"
} catch {
    Write-Host "GitHub HTTPS test failed: $($_.Exception.Message)"
}

Write-Host "============================================"
Write-Host "Telegram Network Diagnostics Complete"
Write-Host "============================================"
