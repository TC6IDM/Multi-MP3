# Dockerized runner for the Multi-MP3 Flask web UI
# Usage: .\run.ps1       # default: build image and run detached
#        .\run.ps1 -Detach:$false   # run in foreground (logs visible)
param(
    [switch]$Detach = $true,
    [switch]$Rebuild = $false,
    [string]$ImageName = "multi-mp3-web"
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (-not (Test-Path "./downloads")) {
    New-Item -ItemType Directory -Path "./downloads" | Out-Null
}

# Check for Docker availability
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if ($null -eq $dockerCmd) {
    Write-Host "Docker not found on this system. Falling back to running the Flask app directly."
    Write-Host "Starting web UI locally..."
    python -u web_app.py
    return
}

# If rebuilding, stop any container using port 5000 or based on this image to avoid port conflicts
function Get-ContainerIdsUsingPort5000 {
    $lines = docker ps --format "{{.ID}} {{.Image}} {{.Ports}}" 2>$null | Select-String ":5000"
    if (-not $lines) { return @() }
    return $lines | ForEach-Object { ($_ -split '\s+')[0] }
}

function Get-ContainerIdsByImage($imageName) {
    $ids = docker ps --filter "ancestor=$imageName" --format "{{.ID}}" 2>$null
    if (-not $ids) { return @() }
    return $ids -split "\r?\n" | Where-Object { $_ -ne "" }
}

$portContainers = Get-ContainerIdsUsingPort5000
$imageContainers = Get-ContainerIdsByImage $ImageName
$allToStop = ($portContainers + $imageContainers) | Select-Object -Unique

if ($Rebuild -and $allToStop.Count -gt 0) {
    Write-Host ("Stopping existing container(s) using port 5000 or image {0}: {1}" -f $ImageName, ($allToStop -join ', '))
    foreach ($c in $allToStop) {
        try { docker stop $c | Out-Null; Write-Host ("Stopped {0}" -f $c) } catch { Write-Warning ("Failed to stop {0}: {1}" -f $c, $_) }
    }
    # Give Docker a moment to release the port
    Start-Sleep -Seconds 2
}

# Only build if image not present or if Rebuild is requested
$imgId = (docker images -q $ImageName) 2>$null
if ($Rebuild -or -not $imgId) {
    Write-Host "Building Docker image: $ImageName"
    if ($Rebuild) {
        docker build --no-cache -t $ImageName .
    } else {
        docker build -t $ImageName .
    }
} else {
    Write-Host "Docker image $ImageName already exists; skipping build. Use -Rebuild to force rebuild."
}

$projectPath = (Get-Location).Path
$envFile = Join-Path $projectPath ".env"

$volumes = @()
$volumes += "-v `"${projectPath}/downloads:/app/downloads`""
if (Test-Path $envFile) { $volumes += "-v `"${projectPath}/.env:/app/.env:ro`"" }

$port = "-p 5000:5000"
$volArg = $volumes -join ' '

# If a container still exists and binds port 5000, stop it so run can bind the port
$existing = Get-ContainerIdsUsingPort5000
if ($existing.Count -gt 0) {
    Write-Host ("Stopping container(s) currently binding port 5000: {0}" -f ($existing -join ', '))
    foreach ($c in $existing) { try { docker stop $c | Out-Null } catch {} }
    Start-Sleep -Seconds 1
}

if ($Detach) {
    Write-Host "Running container (detached). Open http://localhost:5000"
    iex "docker run --rm -d $port $volArg $ImageName"
} else {
    Write-Host "Running container (foreground). Press Ctrl+C to stop."
    iex "docker run --rm $port $volArg $ImageName"
}
