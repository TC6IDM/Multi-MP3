# Build and run the Multi-MP3 Flask web UI inside Docker
param(
    [switch]$Detach = $true,
    [string]$ImageName = "multi-mp3-web"
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host "Building Docker image: $ImageName"
docker build -t $ImageName .

if (-not (Test-Path "./downloads")) {
    New-Item -ItemType Directory -Path "./downloads" | Out-Null
}

$projectPath = (Get-Location).Path
$envFile = Join-Path $projectPath ".env"

$volumes = @()
$volumes += "-v `"${projectPath}/downloads:/app/downloads`""
if (Test-Path $envFile) { $volumes += "-v `"${projectPath}/.env:/app/.env:ro`"" }

# Map port 5000 to host
$port = "-p 5000:5000"

$volArg = $volumes -join ' '

if ($Detach) {
    Write-Host "Running container (detached) — open http://localhost:5000"
    iex "docker run --rm -d $port $volArg $ImageName"
} else {
    Write-Host "Running container (foreground) — press Ctrl+C to stop"
    iex "docker run --rm $port $volArg $ImageName"
}
