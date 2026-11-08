param(
  [ValidateRange(1, 65535)]
  [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

$python = Get-Command python -ErrorAction Stop
$env:HOST = "0.0.0.0"
$env:PORT = [string]$Port

$addresses = [System.Net.Dns]::GetHostAddresses([System.Net.Dns]::GetHostName()) |
  Where-Object {
    $_.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
    -not [System.Net.IPAddress]::IsLoopback($_) -and
    -not $_.ToString().StartsWith("169.254.")
  }

if ($addresses) {
  Write-Host "Энэ компьютертэй ижил хувийн Wi-Fi/сүлжээнд байгаа хэрэглэгчид дараах хаягийг нээнэ:"
  foreach ($address in $addresses) {
    Write-Host "  http://$($address):$Port/"
  }
} else {
  Write-Warning "Дотоод IPv4 хаяг олдсонгүй. Компьютер сүлжээнд холбогдсон эсэхийг шалгана уу."
}

Write-Host "Серверийг зогсоохдоо Ctrl+C дарна уу."
Write-Host "Интернэтэд шууд нийтлэх эсвэл router дээр port forwarding хийхэд энэ HTTP сервер зориулагдаагүй."
& $python.Source server.py
exit $LASTEXITCODE
