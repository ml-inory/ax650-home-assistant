# AX650 Home Assistant Voice

[中文](README.md) | [English](README_EN.md)

这是一个运行在 AX650/AX650N 板端的本地 Home Assistant Assist 语音栈。项目复刻 `rk3576-home-assistant-voice` 的服务布局，并把 RKNN/RKLLM 运行时替换为 AXERA 生态组件：

- ASR：AXERA `ax_asr_api`，以 Wyoming STT 服务暴露在 `10300`
- TTS：AXERA `ax_tts_api`，以 Wyoming TTS 服务暴露在 `10200`
- 唤醒词：`wyoming-openwakeword`，暴露在 `10400`
- 本地对话：AXERA `axllm`，以 OpenAI 兼容 API 暴露在 `8001`
- 可选 Home Assistant 容器 profile

## 板端要求

- AX650 或 AX650N Linux ARM64 开发板
- Docker Engine 和 Docker Compose v2 插件，或 `docker-compose`
- 板端可访问 `/dev` 和 `/soc`，容器会挂载 `/dev:/dev`、`/soc:/soc:ro`
- Python 3、pip、git、curl
- 足够的模型和镜像空间，建议仓库所在分区至少预留 8 GB
- Home Assistant 与板子在同一网络，除非使用本仓库的可选 Home Assistant profile

模型和上游源码缓存不会提交到 git。默认会写入：

```text
models/asr  -> /models/asr
models/tts  -> /models/tts
models/llm  -> /models/llm
vendor/ax_asr_api
vendor/ax_tts_api
vendor/axllm
```

## 一键安装

把仓库放到板端后，在仓库根目录执行：

```bash
bash scripts/install_on_board.sh
```

安装脚本会按顺序完成：

1. 检查 Docker、Compose、Python、git、curl、`/dev`、`/soc` 和磁盘空间。
2. 创建 `models/` 和 `vendor/` 目录。
3. 拉取 ASR/TTS 上游源码缓存，避免 Docker build 阶段反复访问 GitHub。
4. 下载 ASR、TTS、LLM 模型，默认使用 `https://hf-mirror.com`。
5. 构建并启动 ASR、TTS、openWakeWord、LLM 服务。
6. 执行公开端口 smoke check。

常用参数：

```bash
# 只打印将要执行的命令，不改动文件系统
bash scripts/install_on_board.sh --dry-run

# 模型已经准备好时跳过模型下载
bash scripts/install_on_board.sh --skip-models

# 上游源码缓存已经准备好时跳过 vendor 拉取
bash scripts/install_on_board.sh --skip-vendor

# 只检查已经运行的服务，不重新构建/启动
bash scripts/install_on_board.sh --validate-only

# 同时启动本仓库的 Home Assistant profile
bash scripts/install_on_board.sh --with-homeassistant

# 降低磁盘空间门槛，单位 MB
bash scripts/install_on_board.sh --min-free-mb 4096
```

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HF_ENDPOINT` | `https://hf-mirror.com` | Hugging Face 下载端点 |
| `PYTHON` | `python3` | 板端 Python 命令 |
| `COMPOSE` | 自动探测 | `docker compose` 或 `docker-compose` |
| `SMOKE_HOST` | `127.0.0.1` | smoke check 访问的主机 |
| `SMOKE_TIMEOUT` | `3` | smoke check 单项超时时间 |
| `MIN_FREE_MB` | `8192` | 仓库分区最低可用空间 |
| `AX_LLM_RELEASE_URL` | compose 默认值 | AX LLM release binary 下载地址 |

如果板端磁盘空间不足，可以把仓库或 `models/` 目录放在本机挂载到板子的路径上，再在挂载目录中运行安装脚本。

## 手动启动

如果不使用一键安装，也可以分步执行：

```bash
bash scripts/fetch_upstream_sources.sh
bash scripts/download_asr_models.sh
bash scripts/download_tts_models.sh
bash scripts/download_llm_models.sh
docker compose up -d --build
```

启动语音栈加 Home Assistant：

```bash
docker compose --profile homeassistant up -d --build
```

检查状态：

```bash
docker compose ps
docker compose logs -f
python3 scripts/smoke_check.py --host 127.0.0.1 --timeout 3 --public-only
```

如果板端只有旧版 `docker-compose`，把上面的 `docker compose` 换成 `docker-compose`。

## 服务端口

| 服务 | 用途 | 端口 |
| --- | --- | ---: |
| ASR | Wyoming STT adapter for `ax_asr_api` | `10300` |
| TTS | Wyoming TTS adapter for `ax_tts_api` | `10200` |
| openWakeWord | Wyoming wake-word detection | `10400` |
| LLM | OpenAI 兼容 `axllm serve` API | `8001` |



> **国内网络提示**：如果 ghcr.io 拉取镜像失败，Docker Hub 上有相同镜像可通过 DaoCloud 加速：
> ```bash
> docker pull docker.m.daocloud.io/homeassistant/home-assistant:stable
> docker tag docker.m.daocloud.io/homeassistant/home-assistant:stable ghcr.io/home-assistant/home-assistant:stable
> ```

## 语音 Web 桥接 (Voice Web Bridge)

`scripts/voice_web_bridge.py` 是一个独立的 Python 服务，通过 Web 页面实现 PC 麦克风 → AX650 AI → 语音控制设备的完整闭环。

### 功能

- **浏览器语音输入**：PC/手机浏览器录音，转发到 AX650 ASR/LLM/TTS 处理
- **文字输入**：支持直接输入文字指令
- **设备控制面板**：实时显示 HA 设备列表，点击按钮直接控制
- **小米设备接入**：支持局域网扫描、云端登录、手动 Token 输入三种方式
- **LLM 意图理解**：AX650 端侧 Qwen3-0.6B 大模型理解自然语言并执行设备操作
- **Edge TTS 语音播报**：使用 Microsoft 免费 TTS，无需额外部署

### 部署

板端依赖：Python 3 + `aiohttp`

```bash
pip3 install aiohttp

# 生成 HTTPS 自签名证书（浏览器语音需要 HTTPS）
openssl req -x509 -newkey rsa:2048 -keyout /tmp/key.pem -out /tmp/cert.pem -days 365 -nodes -subj "/CN=ax650"

# 启动桥接服务
nohup python3 -u scripts/voice_web_bridge.py \
  --port 8080 \
  --ha-url http://127.0.0.1:8123/api \
  --ha-token YOUR_HA_LONG_LIVED_TOKEN \
  --ssl-cert /tmp/cert.pem --ssl-key /tmp/key.pem \
  > /tmp/bridge.log 2>&1 &
```

### HA Token 获取

HA 页面 → 点击左下角用户名 → **安全** → 最下方 **长期访问令牌** → 创建令牌。

### 使用

浏览器打开 `https://AX650_IP:8080/`（接受自签名证书警告）：

| 功能 | 操作 |
|------|------|
| 🎤 语音输入 | Chrome 浏览器按住"按住说话"按钮 |
| ⌨️ 文字输入 | 输入框打字回车 |
| 📟 设备控制 | 底部设备栏点击按钮开关设备 |
| 🔑 小米接入 | 展开面板 → 扫描局域网 / 云端登录 / 手动输入 Token |
| 🔄 刷新设备 | 点击顶部 🔄 按钮或 30 秒自动刷新 |

### 支持的语音指令

| 指令示例 | 效果 |
|----------|------|
| "打开/关闭 客厅灯" | 开关灯 |
| "打开/关闭 电视" | 开关电视（button.press） |
| "电视大声点/小声点" | 电视音量± |
| "风扇调到最大/最小" | 风扇风速 100%/1% |
| "风扇调到 50%" | 风扇指定百分比 |
| "风扇直吹风/自然风" | 风扇模式切换 |
| "风扇摇头" | 风扇摆风 |

LLM 也会根据系统提示自动理解更自然的表达并输出设备控制指令。

### HA 代理配置（解决 GitHub 被墙）

如果板子无法访问 GitHub（HACS/集成下载失败），在启动 HA 容器时配置代理：

```bash
docker run -d --name homeassistant --restart unless-stopped \
  --privileged --network host \
  -e http_proxy=http://YOUR_PROXY:7890 \
  -e https_proxy=http://YOUR_PROXY:7890 \
  -v /root/homeassistant/config:/config \
  ghcr.io/home-assistant/home-assistant:stable
```

## 小米智能家居接入

本仓库的 Home Assistant profile 预置了 HACS 和 Xiaomi Miot Auto 集成，支持控制小米/米家设备。

### 安装 HACS 和小米集成

HACS：

```bash
# 方法1：使用安装脚本
bash scripts/setup_hacs_miot.sh

# 方法2：手动下载（如 GitHub 被墙）
# 在 PC 下载 https://github.com/hacs/integration/releases/latest/download/hacs.zip
# 上传到板子后解压到 homeassistant/config/custom_components/hacs/
unzip hacs.zip -d /root/homeassistant/config/custom_components/hacs/
docker restart homeassistant
```

Xiaomi Home 集成（推荐，替代 Miot Auto）：

1. HA → 设置 → 设备与服务 → 添加集成 → 搜索 "Xiaomi Home"
2. 扫码登录小米账号，自动发现所有米家设备
3. 支持电视、风扇、灯、空调等设备完整控制

### 启动语音栈 + Home Assistant

```bash
docker compose --profile homeassistant up -d --build
```

### 配置 HA

1. 浏览器打开 `http://AX650_BOARD_IP:8123`，完成 HA 初始化向导。
2. HACS 和 Xiaomi Miot Auto 已预装到 `custom_components/`，无需额外安装。

### 接入小米设备

1. HA UI：**设置 → 设备与服务 → 添加集成**，搜索 `Xiaomi Miot Auto`。
2. 选择 **Login to Mi Account**（扫码或手机号登录）。
3. 登录成功后，所有米家设备自动出现在 HA 中。

### 配置语音助手

1. **设置 → 设备与服务 → 添加集成**，搜索 `OpenAI Conversation`。
2. 填入：`api_key: sk-local`，`base_url: http://127.0.0.1:8001/v1`。
3. **设置 → 语音助手**，创建 Assist Pipeline，将 STT/TTS/唤醒词/对话代理指向 AX650 服务。

### 已支持的设备类型（Miot Auto）

- 灯具、开关、插座
- 空调、风扇、加湿器、空气净化器
- 扫地机器人、窗帘电机
- 传感器（温湿度、门窗、人体）
- 网关及 Zigbee 子设备

详细设备兼容列表见 [Xiaomi Miot Auto 文档](https://github.com/al-one/hass-xiaomi-miot)。

### 语音控制示例

语音栈和 HA 对接后，可自然语言控制小米设备：

```
"打开客厅的灯"
"把卧室空调调到 26 度"
"开始扫地"
"关闭所有窗帘"
```

## Home Assistant 配置

在 Home Assistant 中添加 Wyoming 集成：

| 集成 | Host | Port |
| --- | --- | ---: |
| AX650 ASR | AX650 板子 IP | `10300` |
| AX650 TTS | AX650 板子 IP | `10200` |
| openWakeWord | AX650 板子 IP | `10400` |

本地对话服务使用 OpenAI 兼容配置：

```text
API hostname: AX650 板子 IP
API port: 8001
API path: /v1
API key: sk-local
Model name: axllm-model
```

然后创建或编辑 Assist pipeline，选择 AX650 的 STT、TTS、唤醒词和本地对话服务。

## 模型默认值

| 模块 | 默认模型/路径 |
| --- | --- |
| ASR | `sensevoice`，语言 `auto`，路径 `models/asr/sensevoice` |
| TTS | `kokoro`，语言 `zh`，声音 `zf_xiaoxiao`，路径 `models/tts/kokoro` |
| LLM | `AXERA-TECH/Qwen3-0.6B`，路径 `models/llm/Qwen3-0.6B` |

## 运行时变量

| 服务 | 变量 | 默认值 |
| --- | --- | --- |
| ASR | `AX_ASR_SERVER_BIN` | `/opt/ax_asr_api/install/ax650/asr_server` |
| ASR | `AX_ASR_SERVER_PORT` | `8080` |
| ASR | `AX_ASR_MODEL_PATH` | `/models/asr` |
| ASR | `AX_ASR_ADAPTER_URI` | `tcp://0.0.0.0:10300` |
| TTS | `AX_TTS_SERVER_BIN` | `/opt/ax_tts_api/install/ax650/tts_server` |
| TTS | `AX_TTS_SERVER_PORT` | `8081` |
| TTS | `AX_TTS_MODEL_PATH` | `/models/tts` |
| TTS | `AX_TTS_ADAPTER_URI` | `tcp://0.0.0.0:10200` |
| LLM | `AX_LLM_MODEL_DIR` | `/models/llm/Qwen3-0.6B` |
| LLM | `AX_LLM_PORT` | `8001` |
| LLM | `AX_LLM_RELEASE_URL` | `https://github.com/AXERA-TECH/ax-llm/releases/download/latest/axllm-ax650-linux-arm64` |

调试 adapter 时，如果已经有独立 ASR/TTS HTTP 服务，可以设置 `AX_ASR_ADAPTER_ONLY=1` 或 `AX_TTS_ADAPTER_ONLY=1`，并把 `AX_ASR_HTTP_URL` 或 `AX_TTS_HTTP_URL` 指向对应服务。

## 本地开发验证

安装测试依赖：

```bash
python -m pip install -r requirements-dev.txt
```

运行验证：

```bash
python -m pytest -q
docker-compose config
sh -n scripts/install_on_board.sh
```

对已经启动的板端服务执行 smoke check：

```bash
python3 scripts/smoke_check.py --host AX650_BOARD_IP --timeout 3 --public-only
```

## 故障排查

- `/soc` 不存在：请确认在 AX650 板端运行，且板端系统提供 AX MSP 运行时。
- Docker 拉镜像慢或失败：默认 compose 已使用 DaoCloud Python/Debian 镜像和 USTC apt 源；仍失败时优先检查板端网络和 DNS。
- ASR/TTS 缺少 `asr_server` 或 `tts_server`：确认 `vendor/ax_asr_api`、`vendor/ax_tts_api` 已拉取，容器启动时会尝试在板端编译。
- 模型缺失：重新执行对应下载脚本，或手动把模型放到 `models/asr`、`models/tts`、`models/llm`。
- LLM 没有响应：检查 `models/llm/Qwen3-0.6B` 是否完整，并尝试：

```bash
curl http://AX650_IP:8001/v1/models
curl http://AX650_IP:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"axllm-model","messages":[{"role":"user","content":"你好"}]}'
```
