$bytes = New-Object byte[] 32
$generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$generator.GetBytes($bytes)
$key = [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_")
$generator.Dispose()

try {
    Set-Clipboard -Value $key
}
catch {
    throw "The key was generated but could not be copied to the Windows clipboard. It was not displayed or saved. Run this helper again in Windows PowerShell."
}

Write-Host "STATE_ENCRYPTION_KEY was copied to your Windows clipboard." -ForegroundColor Green
Write-Host "Paste it directly into the GitHub Secret. Do not paste it into chat or a screenshot." -ForegroundColor Yellow
