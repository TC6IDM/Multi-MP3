param([string]$InputFile = "links.example.txt")

$ImageName = "playlist-downloader"

docker build -t $ImageName .

if (-not (Test-Path "./downloads")) {
    New-Item -ItemType Directory -Path "./downloads" | Out-Null
}

$projectPath = (Get-Location).Path
$linksPath = Join-Path $projectPath $InputFile
$downloads = Join-Path $projectPath "downloads"

docker run --rm `
  -v "${projectPath}/.spotdl:/root/.config/spotdl" `
  -v "${linksPath}:/app/input_links.txt:ro" `
  -v "${downloads}:/app/music" `
  -v "${projectPath}/.env:/app/.env:ro" `
  $ImageName `
  "input_links.txt" "/app/music"
