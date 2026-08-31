param(
  [string]$Registry = "tannic666",
  [string]$Image = "watermark-webui",
  [string]$Version = "v1.0.0",
  [string]$Builder = "watermark-webui-multiarch"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$imageName = "$Registry/$Image"

$builders = @(docker buildx ls --format '{{.Name}}')
if ($LASTEXITCODE -ne 0) { throw "Unable to list Docker buildx builders." }

if ($builders -notcontains $Builder) {
  docker buildx create --name $Builder --driver docker-container --use
  if ($LASTEXITCODE -ne 0) { throw "Unable to create the buildx builder: $Builder" }
} else {
  docker buildx use $Builder
  if ($LASTEXITCODE -ne 0) { throw "Unable to select the buildx builder: $Builder" }
}

docker buildx inspect --bootstrap
if ($LASTEXITCODE -ne 0) { throw "Docker BuildKit is not available. Start Docker Desktop and retry." }

docker buildx build `
  --platform linux/amd64,linux/arm64 `
  --file (Join-Path $root "docker/Dockerfile") `
  --tag "$imageName`:$Version" `
  --tag "$imageName`:latest" `
  --push `
  $root

if ($LASTEXITCODE -ne 0) { throw "Multi-architecture image build or push failed." }
Write-Host "Published $imageName`:$Version and $imageName`:latest"
