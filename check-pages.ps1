# Diagnose the existing Quick Tunnel before changing the Pages binding.
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $files = @('-f', 'compose.yaml', '-f', 'compose.pages.yaml')
    function Test-Account([string]$base) {
        try {
            $r = Invoke-WebRequest "$base/api/account" -TimeoutSec 20 -SkipHttpErrorCheck
            $guest = $r.StatusCode -eq 200 -and (($r.Content | ConvertFrom-Json -ErrorAction Stop).PSObject.Properties.Name -contains 'username')
            Write-Host "$base  HTTP $($r.StatusCode)  guest JSON=$guest"
            return $guest
        } catch { Write-Host "$base  $($_.Exception.Message)"; return $false }
    }
    if (-not (Test-Account 'http://127.0.0.1:8000')) { throw 'Local FastAPI failed. Inspect docker compose logs --tail 50 api db.' }
    foreach ($file in 'districts.py','fetch_macro.py','molit.py') {
        $localHash = (Get-FileHash -Algorithm SHA256 "backend/$file").Hash.ToLowerInvariant()
        $remote = docker compose exec -T updater sha256sum "/app/$file"
        if ($LASTEXITCODE -eq 0) {
            $match = $localHash -eq (($remote -split '\s+')[0])
            Write-Host "updater $file identical to working tree (including line endings): $match"
        }
    }
    $public = 'https://jipfit-48v.pages.dev'
    if (Test-Account $public) {
        $r = Invoke-WebRequest "$public/api/complexes?include_sort=true" -TimeoutSec 90 -SkipHttpErrorCheck
        if ($r.StatusCode -ne 200 -or -not $r.Content.TrimStart().StartsWith('[')) { throw "Public list failed (HTTP $($r.StatusCode)) although guest account succeeded. Inspect API logs." }
        Write-Host 'PASS public guest and apartment list'
        return
    }
    docker compose @files up -d --no-recreate --no-deps tunnel
    if ($LASTEXITCODE) { throw 'Unable to start the tunnel' }
    $origin = $null
    for ($n=0; $n -lt 10; $n++) {
        $log = docker compose @files logs --no-color --tail 500 tunnel 2>&1 | Out-String
        $urls = [regex]::Matches($log, 'https://[a-z0-9-]+\.trycloudflare\.com')
        if ($urls.Count) {
            $origin = $urls[$urls.Count-1].Value
            if (Test-Account $origin) { break }
        }
        Start-Sleep 2
    }
    if (-not $origin -or -not (Test-Account $origin)) { throw 'Quick Tunnel did not return guest JSON. Inspect tunnel logs and connectivity to api:8000.' }
    if (Test-Account $public) { Write-Host 'Pages guest endpoint recovered. Run python verify_public.py'; return }
    Write-Host "Current working tunnel: $origin"
    Write-Host 'Check Cloudflare Pages > jipfit-48v > Settings > Variables and Secrets > Production > API_ORIGIN against the URL above, then redeploy and run python verify_public.py.'
} finally { Pop-Location }
