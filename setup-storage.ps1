$ErrorActionPreference = 'Stop'
foreach ($setting in @('WANG_POSTGRES_PASSWORD', 'WANG_REDIS_PASSWORD')) {
    $existing = [Environment]::GetEnvironmentVariable($setting, 'User')
    if (-not $existing) {
        $existing = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
        [Environment]::SetEnvironmentVariable($setting, $existing, 'User')
    }
    [Environment]::SetEnvironmentVariable($setting, $existing, 'Process')
}
$env:DATABASE_URL = 'postgresql://wang:' + $env:WANG_POSTGRES_PASSWORD + '@127.0.0.1:15432/wang'
$env:REDIS_URL = 'redis://:' + $env:WANG_REDIS_PASSWORD + '@127.0.0.1:16379/0'
[Environment]::SetEnvironmentVariable('DATABASE_URL', $env:DATABASE_URL, 'User')
[Environment]::SetEnvironmentVariable('REDIS_URL', $env:REDIS_URL, 'User')
Write-Host 'Storage credentials configured in user environment; no credential files created.'
