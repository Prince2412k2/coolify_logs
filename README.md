# Docker Log Gateway

A lightweight log gateway for Docker hosts:

- Admin UI to manage API keys + container access
- REST + WebSocket API for live logs
- User web UI for viewing logs with an API key
- Cross-platform CLI installable via `pipx` or `uv`

## Quick Start (Docker)

```bash
docker compose up --build
```

Then open:

- User UI: `http://localhost:8080/`
- Admin UI: `http://localhost:8080/admin` (defaults: `admin` / `changeme`)

## Environment Variables

All have defaults:

- `ADMIN_USERNAME` (default `admin`)
- `ADMIN_PASSWORD` (default `changeme`)
- `HOST` (default `0.0.0.0`)
- `PORT` (default `8080`)
- `DB_PATH` (default `/data/db.sqlite`)
- `LOG_LEVEL` (default `info`)

Optional rate limiting:

- `RATE_LIMIT_ENABLED` (default `true`)
- `RATE_LIMIT_PER_MINUTE` (default `120`) - applied per IP to `/api/*` and WebSocket connects
- `RATE_LIMIT_BURST` (default `60`)
- `RATE_LIMIT_ADMIN_LOGIN_PER_MINUTE` (default `20`) - per IP for `POST /admin/login`
- `RATE_LIMIT_ADMIN_LOGIN_BURST` (default `10`)
- `TRUST_PROXY_HEADERS` (default `true`) - trusts `X-Forwarded-For` / `X-Real-IP` only when the direct peer IP is private/loopback

## API

- `GET /api/containers` (Bearer token required)
- `WS /api/logs/{container}`
  - Either supply `Authorization: Bearer <token>` header (CLI)
  - Or send `{ "type": "auth", "token": "...", "tail": 100 }` as the first message (browser)

Errors are returned as JSON:

```json
{ "error": "message" }
```

WebSocket auth/container errors are sent as:

```json
{ "type": "error", "message": "..." }
```

## CLI

### Installation Options

The CLI can be installed in several ways:

#### Recommended: pipx or uv (isolated environments)
```bash
# pipx
pipx install ./log-gateway

# uv
uv tool install ./log-gateway
```

#### Traditional pip install
```bash
# User install (no admin needed)
pip install --user ./log-gateway

# System-wide install (requires sudo)
sudo pip install ./log-gateway

# Editable/develop mode (for development)
pip install -e ./log-gateway
```

#### From source (any of the above work after cloning)
```bash
git clone <repository-url>
cd log-gateway
# Then use any install method above
```

#### From Git directly
```bash
# pipx
pipx install git+<repository-url>.git

# pip
pip install git+<repository-url>.git
```

#### Using other Python tools
```bash
# Poetry
poetry add ./log-gateway

# Pipenv
pipenv install ./log-gateway
```

### Usage

```bash
# Configure CLI (stores server URL and API key)
logcli auth set --server http://localhost:8080 --key <API_KEY>

# View stored configuration
logcli auth show

# List available containers
logcli containers

# Stream logs from a container
logcli logs my-container --tail 200 --grep "ERROR|WARN"

# Follow logs from start (equivalent to `docker logs -f`)
logcli logs my-container --follow
```

### Shell Completion

Install tab completion for your shell:
```bash
logcli --install-completion
```

Container name tab-completion:
- `logcli logs <TAB>` autocompletes container names by calling `/api/containers` using your stored config.

### Configuration File

The CLI stores configuration in:
- Linux/macOS: `~/.config/logcli/config.json`
- Windows: `%APPDATA%\logcli\config.json`

Example config:
```json
{
  "server_url": "http://localhost:8080",
  "api_key": "your-api-key-here"
}
```
