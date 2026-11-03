$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not $env:DEEPSEEK_API_KEY) {
    $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
}
python -m uvicorn wang.app:app --host 127.0.0.1 --port 8000
