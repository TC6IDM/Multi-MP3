<#
Runs the CLI downloader inside Docker using Dockerfile.cli (ENTRYPOINT: python main.py)
Usage:
  .\run_old_docker.ps1 -InputFile links.txt
  .\run_old_docker.ps1 -InputFile links.txt -OutputDir downloads -Rebuild

This will:
 - Build image multi-mp3-cli from Dockerfile.cli (if missing or -Rebuild)
 - Mount the input file as /app/input_links.txt (read-only)
 - Mount the host OutputDir as /app/music
 - Mount .env if present
 - Run container in foreground (so logs print to this terminal)
#>
param(
    [Parameter(Mandatory=$true)][string]$InputFile,
    [string]$OutputDir = "downloads",
    [switch]$Rebuild = $false,
    [string]$ImageName = "multi-mp3-cli"
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

# Resolve input path
$inputPath = Resolve-Path -Path $InputFile -ErrorAction SilentlyContinue
if (-not $inputPath) {
    Write-Error "Input file not found: $InputFile"
    exit 1
}
$inputPath = $inputPath.Path

# Resolve output dir
if (-not [System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir = Join-Path $here $OutputDir
}
if (-not (Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir | Out-Null }

# Check Docker
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if ($null -eq $dockerCmd) {
    Write-Error "Docker not found on this system. Install Docker or run the CLI locally with run_old.ps1"
    exit 1
}

# Build image if missing or requested
$imgId = (docker images -q $ImageName) 2>$null
if ($Rebuild -or -not $imgId) {
    Write-Host "Building Docker CLI image: $ImageName (this may take a bit)"
    docker build -f Dockerfile.cli -t $ImageName .
} else {
    Write-Host "Using existing image $ImageName"
}

$envFile = Join-Path $here ".env"
$envMount = ""
if (Test-Path $envFile) { $envMount = "-v `"${envFile}:/app/.env:ro`"" }

$inputMount = "-v `"${inputPath}:/app/input_links.txt:ro`""
$downloadsMount = "-v `"${OutputDir}:/app/music`""

# Run in foreground (so output prints here)
$cmd = "docker run --rm -it $inputMount $downloadsMount $envMount $ImageName input_links.txt /app/music"
Write-Host "Running: $cmd"
iex $cmd
