param([string]$WebOrigin = "http://127.0.0.1:5173")
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$out = Join-Path $root "pages-upload"
New-Item -ItemType Directory -Path $out -Force | Out-Null
foreach ($name in @("index.html", "app.js", "style.css", "config.js")) {
    $asset = Invoke-WebRequest "$WebOrigin/$name" -UseBasicParsing
    if ($name -ne "index.html" -and $asset.Headers["Content-Type"] -match "text/html") {
        throw "Missing web asset: $name"
    }
    [IO.File]::WriteAllBytes((Join-Path $out $name), $asset.RawContentStream.ToArray())
}
$sourceData = Join-Path $root "frontend/public/data"
$manifest = Join-Path $sourceData "manifest.json"
if (Test-Path $manifest) {
    $targetData = Join-Path $out "data"
    if (Test-Path $targetData) { Remove-Item $targetData -Recurse -Force }
    Copy-Item $sourceData $targetData -Recurse
    Write-Host "Included versioned JSON snapshots"
} else {
    Write-Warning "No JSON manifest; web will use API fallback."
}
Copy-Item (Join-Path $root "frontend/_worker.js") $out
Write-Host $out
