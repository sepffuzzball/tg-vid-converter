# TG Video Converter

A lightweight Docker service that receives video files (e.g., from Home Assistant / Unifi Protect automations), converts them to Telegram-compatible MP4 using FFmpeg, and sends them directly to one or more Telegram chats.

## Features

- **FastAPI-based REST API** with two endpoints:
  - `POST /convert` — upload a video file directly
  - `POST /convert-url` — provide a video URL to download and convert
- **Automatic FFmpeg conversion** to H.264/AAC MP4 with configurable CRF and preset
- **Multi-chat support** — send the same video to multiple Telegram chats
- **Subnet-based IP filtering** — restrict access to your local network
- **Non-root Docker container** with health checks
- **Multi-architecture images** (amd64 + arm64) published via GitHub Container Registry

## Quick Start

### Using Docker Compose (recommended)

1. Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
# Edit .env with your Telegram bot token and chat IDs
```

2. Start the service:

```bash
docker compose up -d
```

3. Verify it's running:

```bash
curl http://localhost:8080/health
```

### Using Docker directly

```bash
docker build -t tg-vid-converter .

docker run -d \
  --name tg-vid-converter \
  --restart unless-stopped \
  -p 8080:8080 \
  -e TELEGRAM_BOT_TOKEN="your-bot-token" \
  -e TELEGRAM_CHAT_IDS="chat-id-1,chat-id-2" \
  -e ALLOWED_SUBNET="192.168.1.0/24" \
  tg-vid-converter
```

### Using the pre-built image from GitHub Packages

```bash
docker pull ghcr.io/<owner>/tg-vid-converter2:latest

docker run -d \
  --name tg-vid-converter \
  --restart unless-stopped \
  -p 8080:8080 \
  -e TELEGRAM_BOT_TOKEN="your-bot-token" \
  -e TELEGRAM_CHAT_IDS="chat-id-1,chat-id-2" \
  -e ALLOWED_SUBNET="192.168.1.0/24" \
  ghcr.io/<owner>/tg-vid-converter2:latest
```

Replace `<owner>` with your GitHub username or organization.

## Configuration

All configuration is done via environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_IDS` | Yes | — | Comma-separated list of chat IDs to send videos to |
| `ALLOWED_SUBNET` | No | (empty = all) | CIDR subnet to restrict API access (e.g., `192.168.1.0/24`) |
| `FFMPEG_PATH` | No | `ffmpeg` | Path to ffmpeg binary |
| `PORT` | No | `8080` | HTTP port to listen on |

### Getting Your Telegram Chat ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram — it will reply with your chat ID.
2. For groups, add the bot to the group and send `/start` — the chat ID will appear in the bot's logs or you can use the Telegram Bot API `getUpdates` endpoint.

## API Reference

### `GET /health`

Health check endpoint.

**Response:**

```json
{
  "status": "ok",
  "telegram_configured": true
}
```

### `POST /convert`

Upload a video file for conversion and optional Telegram delivery.

**Content-Type:** `multipart/form-data`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `file` | file | **required** | Video file to convert |
| `width` | int | — | Output width (maintains aspect ratio if only one dimension set) |
| `height` | int | — | Output height |
| `crf` | int | `23` | H.264 CRF quality (lower = better, 0–51) |
| `preset` | string | `medium` | FFmpeg encoding preset (`ultrafast` … `veryslow`) |
| `format` | string | `mp4` | Output container format |
| `send_to_telegram` | bool | `true` | Whether to send to Telegram |
| `caption` | string | — | Caption for the Telegram message |
| `chat_ids` | string | — | Override chat IDs (comma-separated) |

**Example with curl:**

```bash
curl -X POST http://localhost:8080/convert \
  -F "file=@/path/to/video.mp4" \
  -F "caption=Motion detected!" \
  -F "crf=28" \
  -F "preset=fast"
```

**Response:**

```json
{
  "message": "Video converted and sent to Telegram.",
  "output_size": 1234567,
  "results": [
    {"chat_id": "-1001234567890", "success": true}
  ]
}
```

### `POST /convert-url`

Download a video from a URL, convert, and send to Telegram.

**Content-Type:** `multipart/form-data`

Same parameters as `/convert`, except replace `file` with:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | string | **required** | URL to download the video from |

**Example with curl:**

```bash
curl -X POST http://localhost:8080/convert-url \
  -F "url=https://example.com/video.mp4" \
  -F "caption=Motion detected!" \
  -F "chat_ids=-1001234567890"
```

## Home Assistant Automation Setup

### Prerequisites

1. **Unifi Protect** integration configured in Home Assistant
2. **TG Video Converter** running and accessible from your Home Assistant instance
3. A Telegram bot token and chat ID

### Option A: Using a RESTful Notification (file upload)

This method downloads the Unifi Protect video clip and uploads it to the converter.

Add this to your `configuration.yaml`:

```yaml
rest_command:
  send_video_to_telegram:
    url: "http://<CONVERTER_IP>:8080/convert"
    method: POST
    content_type: "multipart/form-data; boundary=----FormBoundary"
    payload: >-
      ------FormBoundary
      Content-Disposition: form-data; name="file"; filename="video.mp4"
      Content-Type: video/mp4

      {{ states.camera.your_unifi_camera.attributes.video }}
      ------FormBoundary
      Content-Disposition: form-data; name="caption"

      Motion detected on {{ states.camera.your_unifi_camera.name }}
      ------FormBoundary--
```

> **Note:** The RESTful notification approach with binary payloads can be tricky. Option B is generally more reliable.

### Option B: Using a Shell Command + cURL (recommended)

This approach uses a shell command to download the video from Unifi Protect and upload it to the converter.

1. Add a shell command to your `configuration.yaml`:

```yaml
shell_command:
  send_video_to_telegram: >-
    curl -X POST http://<CONVERTER_IP>:8080/convert
    -F "file=@{{ video_path }}"
    -F "caption={{ caption }}"
    -F "crf=28"
    -F "preset=fast"
```

2. Create an automation in `automations.yaml`:

```yaml
- alias: "Send Unifi Protect Clip to Telegram"
  description: "When motion is detected, convert and send the video clip to Telegram"
  trigger:
    - platform: state
      entity_id: binary_sensor.your_unifi_camera_motion
      to: "on"
  action:
    - service: shell_command.send_video_to_telegram
      data:
        video_path: "/config/www/recordings/latest_clip.mp4"
        caption: "Motion detected on Front Camera"
  mode: single
```

### Option C: Using the /convert-url Endpoint

If your Unifi Protect instance exposes video URLs that are accessible from the converter container, you can use the URL-based endpoint. This is useful when Unifi Protect provides a direct video URL with Bearer token authentication:

```yaml
rest_command:
  send_video_url_to_telegram:
    url: "http://<CONVERTER_IP>:8080/convert-url"
    method: POST
    content_type: "application/x-www-form-urlencoded"
    payload: >-
      url={{ video_url }}&caption={{ caption }}&crf=28&preset=fast&auth_token={{ protect_token }}

automation:
  - alias: "Send Unifi Protect URL to Telegram"
    trigger:
      - platform: state
        entity_id: binary_sensor.your_unifi_camera_motion
        to: "on"
    action:
      - service: rest_command.send_video_url_to_telegram
        data:
          video_url: "{{ state_attr('camera.your_unifi_camera', 'video_url') }}"
          caption: "Motion detected on {{ state_attr('camera.your_unifi_camera', 'friendly_name') }}"
          protect_token: "{{ states('sensor.unifi_protect_auth_token') }}"
```

The `auth_token` parameter will be sent as a Bearer token in the HTTP request headers when fetching the video URL. This is required for Unifi Protect clips that need authentication.

### Tips for Home Assistant Integration

- **Network access:** Make sure the converter container is reachable from Home Assistant. If using Docker networking, use the container name. If on the same host, use the host IP or `host.docker.internal` (Docker Desktop).
- **ALLOWED_SUBNET:** Set this to your Home Assistant's subnet (e.g., `192.168.1.0/24`) to prevent unauthorized access.
- **Video size:** Unifi Protect clips can be large. Consider using a higher CRF value (28–30) and `preset=fast` for quicker conversions.
- **Timeout:** The default FFmpeg timeout is 300 seconds. For very long clips, you may need to adjust this.

## GitHub Actions CI/CD

The repository includes a GitHub Actions workflow (`.github/workflows/build.yml`) that:

- Builds on every push to `main`/`master` and on version tags (`v*`)
- Builds for **linux/amd64** and **linux/arm64**
- Pushes the image to **GitHub Container Registry (ghcr.io)**
- Tags images with: branch name, semantic version (for tags), and commit SHA

### Publishing a Release

```bash
git tag v1.0.0
git push origin v1.0.0
```

This will build and publish `ghcr.io/<owner>/tg-vid-converter2:1.0.0`.

## Development

### Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

### Running Tests

```bash
# Health check
curl http://localhost:8080/health

# Convert a local file (don't send to Telegram)
curl -X POST http://localhost:8080/convert \
  -F "file=@test-video.mp4" \
  -F "send_to_telegram=false"
```

## License

MIT
