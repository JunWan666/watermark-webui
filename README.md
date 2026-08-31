<div align="center">
  <img src="./docs/logo/logo.svg" alt="Invisible Watermark logo" width="80" />
  <h1>Invisible Watermark WebUI</h1>
  <p>本地隐形水印嵌入、提取与记录管理工作台</p>
  <p>基于 Flask、Galaxy UI 和 <a href="https://github.com/guofei9987/blind_watermark">blind_watermark</a>，图片与账户数据默认只保存在本地。</p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.11-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11" />
    <img src="https://img.shields.io/badge/Flask-3.x-000000?style=for-the-badge&logo=flask&logoColor=white" alt="Flask" />
    <img src="https://img.shields.io/badge/Galaxy%20UI-CDN-2D6CDF?style=for-the-badge" alt="Galaxy UI" />
    <img src="https://img.shields.io/badge/Docker-ready-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker ready" />
  </p>
  <p>
    <img src="https://img.shields.io/badge/SQLite-local-003B57?style=flat-square&logo=sqlite&logoColor=white" alt="SQLite local storage" />
    <img src="https://img.shields.io/badge/Mobile-responsive-12A781?style=flat-square" alt="Responsive mobile layout" />
    <img src="https://img.shields.io/badge/Storage-local-637089?style=flat-square" alt="Local storage" />
  </p>
</div>

## 项目简介

Invisible Watermark WebUI 是一个面向个人和小团队的本地图片水印工作台。它将 `blind_watermark` 算法封装成清晰的 Web 操作界面，用于在图片中嵌入隐形文字水印、从图片中提取水印，并保存可检索的处理记录。

图片处理在后端本地执行，不会自动上传到云端。应用提供首次访问初始化账户、登录鉴权、账户信息修改、SQLite 记录、原图/结果图下载和移动端适配。

## 界面预览

<table>
  <tr>
    <td align="center"><strong>登录与初始化</strong><br><img src="./docs/images/login.png" alt="登录与初始化页面" width="280" /></td>
    <td align="center"><strong>嵌入水印工作台</strong><br><img src="./docs/images/embed-workspace.png" alt="嵌入水印工作台" width="280" /></td>
    <td align="center"><strong>嵌入记录详情</strong><br><img src="./docs/images/embed-detail.png" alt="嵌入记录详情" width="280" /></td>
  </tr>
  <tr>
      <td align="center"><strong>处理记录</strong><br><img src="./docs/images/history-list.png" alt="处理记录列表" width="280" /></td>
    <td align="center"><strong>提取水印工作台</strong><br><img src="./docs/images/extract-workspace.png" alt="提取水印工作台" width="280" /></td>
    <td align="center"><strong>提取记录详情</strong><br><img src="./docs/images/extract-detail.png" alt="提取记录详情" width="280" /></td>
  </tr>
</table>


## 功能特性

- **嵌入水印**：上传 PNG/JPG 图片，输入水印文字和参数，输出带隐形水印的 PNG。
- **提取水印**：上传带水印的图片，填写嵌入时的 `wm_shape` 和两个密码，提取原始文字内容。
- **文本复制**：提取结果中的水印文字可以直接复制；结果 SVG 仅作为预览和下载用的展示图。
- **图片预览**：原图和结果图支持点击放大，查看器内支持下载。
- **处理记录**：保存原图、水印文字、`wm_shape`、两个密码、备注、文件尺寸和结果图，支持模糊搜索、详情、下载和删除。
- **本地鉴权**：首次没有用户时显示初始化注册；完成初始化后只显示登录页。账户设置支持修改用户名和密码。
- **响应式布局**：桌面端使用侧栏导航，移动端使用底部导航，核心工作区适配窄屏。
- **本地存储**：SQLite 保存元数据，图片保存在 `data/uploads` 和 `data/results`，可通过 Docker volume 持久化。
- **文件限制**：默认单个上传文件最大 50 MB，支持浏览器拖拽上传。

## 水印参数

`图片密码` 和 `水印密码` 都需要在提取时与嵌入时保持一致：

- **图片密码**：控制图像处理过程中的像素排列方式。
- **水印密码**：控制水印编码过程中的排列方式。
- **wm_shape**：嵌入时返回的水印长度，提取时必须填写同一个值。

默认两个密码都是 `1234`。生产或多人共用环境建议使用独立参数并妥善保存。

## Docker 部署

镜像支持 `linux/amd64` 和 `linux/arm64`：

- `tannic666/watermark-webui:v1.0.0`
- `tannic666/watermark-webui:latest`

### Docker Compose（推荐）

根目录的 `docker-compose.yml` 默认将数据保存到 `./data`：

```bash
docker compose pull
docker compose up -d
```

### Docker Run

多行命令：

```bash
docker run -d \
  --name watermark-webui \
  --restart unless-stopped \
  -p 8655:8655 \
  -e WATERMARK_SECRET_KEY=replace-with-a-long-random-value \
  -v "${PWD}/data:/app/data" \
  tannic666/watermark-webui:v1.0.0
```

单行命令：

```bash
docker run -d --name watermark-webui --restart unless-stopped -p 8655:8655 -e WATERMARK_SECRET_KEY=replace-with-a-long-random-value -v "${PWD}/data:/app/data" tannic666/watermark-webui:v1.0.0
```

启动后访问 `http://localhost:8655`。账户、数据库和图片均保存在本地 `data/` 目录。双架构镜像构建说明见 [`docker/README.md`](./docker/README.md)。镜像会自动创建本地数据子目录并处理容器用户权限，业务进程以非 root 的 `watermark` 用户运行。

## 本地运行

### 环境要求

- Python 3.11+
- OpenCV 可用的运行环境

### 安装与启动

```bash
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
# .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
python app.py
```

生产环境可以使用 Gunicorn：

```bash
gunicorn --bind 0.0.0.0:8655 --workers 2 --timeout 120 app:app
```

## 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WATERMARK_SECRET_KEY` | `change-this-local-secret` | Flask 会话签名密钥，部署时应替换 |
| `WATERMARK_DATA_DIR` | 项目目录下的 `data` | SQLite、上传图和结果图的根目录 |
| `WATERMARK_COOKIE_SECURE` | `false` | HTTPS 部署时可设置为 `true` |

## API

除 `/api/health` 外，API 均需要先登录。图片接口使用 `multipart/form-data`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/auth/status` | 查询登录状态和是否需要初始化 |
| POST | `/api/auth/register` | 无用户时初始化注册 |
| POST | `/api/auth/login` | 本地会话登录 |
| POST | `/api/auth/logout` | 退出登录 |
| PATCH | `/api/auth/account` | 修改用户名或密码 |
| POST | `/api/embed` | 嵌入文字水印并创建记录 |
| POST | `/api/extract` | 提取文字水印并创建记录 |
| GET | `/api/records` | 按 `q` 模糊搜索记录，可用 `operation=embed\|extract` 筛选 |
| GET | `/api/records/<id>` | 获取记录详情和参数 |
| PATCH | `/api/records/<id>` | 更新备注 |
| DELETE | `/api/records/<id>` | 删除记录及关联图片 |
| GET | `/api/records/<id>/original` | 查看或下载原图，追加 `?download=1` 下载 |
| GET | `/api/records/<id>/result` | 查看或下载结果图，追加 `?download=1` 下载 |

## 目录结构

```text
watermark-webui/
├── app.py                    # Flask 后端、鉴权、SQLite 和图片处理 API
├── static/
│   ├── index.html             # Galaxy UI 单页前端
│   └── logo.svg               # Web 页面运行时 Logo
├── docs/
│   ├── images/                # README 界面截图
│   └── logo/                  # README 使用的品牌 Logo
├── data/                      # 本地运行数据目录（仅保留 .gitkeep）
│   ├── uploads/               # 用户上传的原图
│   ├── results/               # 处理结果和提取内容 SVG
│   └── watermark.db           # SQLite 数据库
├── docker/
│   ├── Dockerfile              # 生产镜像构建文件
│   ├── build-push.ps1          # amd64/arm64 镜像构建与推送脚本
│   └── README.md               # Docker 构建、部署与发布说明
├── docker-compose.yml          # Compose 部署配置
├── requirements.txt           # Python 依赖
├── COMMIT_RULES.md             # 仓库提交信息规范
├── .dockerignore
├── .gitignore
└── README.md                   # 项目说明
```

## 注意事项

- 提取时必须使用嵌入时对应的 `wm_shape`、图片密码和水印密码。
- 嵌入结果统一输出 PNG；对图片进行压缩或二次处理可能影响水印提取。
- `data/` 包含账户、密码哈希、参数和图片文件，已加入 `.gitignore`，请使用 Docker volume 或备份目录保存。
- 默认会话密钥仅适合本地开发，部署前请设置足够长度的 `WATERMARK_SECRET_KEY`。
- 当前前端通过 CDN 加载 Galaxy UI；离线环境需要将 Galaxy 资源改为本地静态文件。

## 相关项目

- 应用仓库：[JunWan666/watermark-webui](https://github.com/JunWan666/watermark-webui)
- 算法库：[guofei9987/blind_watermark](https://github.com/guofei9987/blind_watermark)
