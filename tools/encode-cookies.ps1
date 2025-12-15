param(
  [string]$CookiesPath = "backend\\cookies.txt"
)

$full = Resolve-Path $CookiesPath -ErrorAction Stop
$bytes = [System.IO.File]::ReadAllBytes($full)
$b64 = [Convert]::ToBase64String($bytes)

Write-Output "Set this Render secret env var:"
Write-Output "  Key:   YTDLP_COOKIES_B64"
Write-Output "  Value: $b64"
