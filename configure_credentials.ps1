# Run locally in the PowerShell window from which you will launch the bot.
# No file is written and live order execution is not enabled by this script.
$taskSecret = Read-Host 'POLYMARKET_PRIVATE_KEY (saisie masquée)' -AsSecureString
try {
    $taskKeyValue = [System.Net.NetworkCredential]::new('', $taskSecret).Password
    [Environment]::SetEnvironmentVariable('POLYMARKET_PRIVATE_KEY', $taskKeyValue, 'Process')
} finally {
    $taskKeyValue = $null
    $taskSecret.Dispose()
}
$taskFunder = Read-Host 'POLYMARKET_FUNDER (adresse du wallet Polymarket)'
[Environment]::SetEnvironmentVariable('POLYMARKET_FUNDER', $taskFunder, 'Process')
[Environment]::SetEnvironmentVariable('POLYMARKET_SIGNATURE_TYPE', '3', 'Process')
[Environment]::SetEnvironmentVariable('POLYMARKET_API_URL', 'https://clob.polymarket.com', 'Process')
Write-Host 'Variables renseignées pour cette session PowerShell. Exécution réelle non activée.'
