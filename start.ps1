$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not $env:DEEPSEEK_API_KEY) {
    $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
}
foreach ($setting in @('DATABASE_URL','REDIS_URL','WANG_REDIS_PREFIX','WANG_ADMIN_TOKEN','WANG_NATIVE_GATEWAY_URL','WANG_NATIVE_GATEWAY_TOKEN','WANG_TAOBAO_GATEWAY_URL','WANG_TAOBAO_GATEWAY_TOKEN','WANG_DOUYIN_GATEWAY_URL','WANG_DOUYIN_GATEWAY_TOKEN')) {
    if (-not [Environment]::GetEnvironmentVariable($setting, 'Process')) {
        [Environment]::SetEnvironmentVariable($setting, [Environment]::GetEnvironmentVariable($setting, 'User'), 'Process')
    }
}
python -m uvicorn wang.app:create_app --factory --host 127.0.0.1 --port 8000
