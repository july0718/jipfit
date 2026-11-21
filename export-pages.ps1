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
Copy-Item (Join-Path $root "frontend/_worker.js") $out
Copy-Item (Join-Path $root "frontend/public/metadata") $out -Recurse -Force
Write-Host $out
