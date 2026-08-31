# Docker 部署与发布

本项目使用根目录作为 Docker build context，生产 Dockerfile 放在 `docker/Dockerfile`。镜像支持 Linux `amd64` 和 `arm64`，发布标签为：

- `tannic666/watermark-webui:v1.0.0`
- `tannic666/watermark-webui:latest`

## 本地 Compose

```bash
docker compose up -d
docker compose logs -f watermark-webui
```

Compose 默认将 SQLite、上传图片和处理结果保存到项目根目录的 `data/`，服务地址为 `http://localhost:8655`。镜像启动时会自动创建 `data/uploads`、`data/results` 并处理目录权限，业务进程以非 root 的 `watermark` 用户运行。

可在项目根目录创建 `.env` 覆盖配置：

```dotenv
WATERMARK_PORT=8655
WATERMARK_SECRET_KEY=replace-with-a-long-random-value
WATERMARK_COOKIE_SECURE=false
```

## 构建并推送双架构镜像

先启动 Docker Desktop，确认 `docker info` 可以访问 Linux daemon，并完成 `docker login`。PowerShell 执行：

```powershell
.\docker\build-push.ps1
```

脚本会创建或复用 `watermark-webui-multiarch` builder，并使用 BuildKit 一次构建并推送 `linux/amd64`、`linux/arm64` 两个平台的 `v1.0.0` 和 `latest` 标签。

手动执行等价命令：

```bash
docker buildx create --name watermark-webui-multiarch --driver docker-container --use
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -f docker/Dockerfile \
  -t tannic666/watermark-webui:v1.0.0 \
  -t tannic666/watermark-webui:latest \
  --push .
```

## 健康检查

容器内健康检查访问 `/api/health`。查看状态：

```bash
docker inspect --format='{{json .State.Health}}' watermark-webui
```
