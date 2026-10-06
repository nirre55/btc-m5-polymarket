param(
  [ValidateSet('start','stop','status','report','probe','halt','resume','test')][string]$Command='status',
  [ValidateSet('prepare','paper','live')][string]$Mode='prepare'
)
$taskPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { $taskPython = 'python' }
if ($Command -eq 'test') {
  Push-Location $PSScriptRoot
  try { & $taskPython -m unittest -v test_bot } finally { Pop-Location }
} else {
  & $taskPython (Join-Path $PSScriptRoot 'bot.py') $Command --mode $Mode
}
exit $LASTEXITCODE
