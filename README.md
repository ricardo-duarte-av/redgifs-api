# redgifs-api

A small HTTP service that exposes redgifs.com user feeds and gifs as a simple JSON API.

redgifs.com has no public API. This service talks to the same backend the website uses
(`api.redgifs.com/v2`), using an anonymous token that it fetches and refreshes automatically.
That backend is undocumented and may change without notice.

## Running

```sh
docker compose up -d
```

The service listens on port `8080` (container port `8000`). Interactive API docs are at
`http://localhost:8080/docs`.

To build the image locally instead of pulling it, uncomment `build: .` in `docker-compose.yml`
and run `docker compose up -d --build`.

### Configuration

| Variable | Default | Description |
|---|---|---|
| `REDGIFS_USER_AGENT` | Firefox UA string | User-Agent sent to redgifs. The token is bound to it. |
| `ALLOW_PROXY` | `true` | Allow `/download?proxy=true` to stream video through this service. |
| `LOG_LEVEL` | `info` | Python log level. |

## Endpoints

### `GET /users/{username}`

A user's profile and one page of their gifs.

| Param | Default | Values |
|---|---|---|
| `order` | `latest` | `latest`, `oldest`, `trending`, `top`, `top7`, `top28` |
| `page` | `1` | 1-based page number |
| `limit` | `20` | 1–100 |

```sh
curl 'http://localhost:8080/users/susanna?limit=5'
```

```json
{
  "user": {"name": "susanna", "url": "...", "followers": 104819, "gifs": 6150, "profile_image": "...", ...},
  "page": 1, "pages": 1124, "total": 5618,
  "gifs": [
    {
      "id": "hotpinkacrobatickitfox",
      "url": "https://www.redgifs.com/watch/hotpinkacrobatickitfox",
      "user": "susanna",
      "created": 1790840090,
      "duration": 6.466, "width": 1080, "height": 1920, "has_audio": true,
      "tags": ["..."],
      "urls": {"hd": "...mp4", "sd": "...-mobile.mp4", "silent": "...-silent.mp4", "poster": "...jpg", "thumbnail": "...jpg"}
    }
  ]
}
```

Unknown users return `404` with `{"error": {"code": "UserNotFound", ...}}`.

### `GET /gif/{ref}`

Metadata and media URLs for one gif. `ref` can be a gif id or any redgifs URL
(`https://www.redgifs.com/watch/<id>`, `/ifr/<id>`, or a `media.redgifs.com` file URL).

```sh
curl http://localhost:8080/gif/hotpinkacrobatickitfox
curl http://localhost:8080/gif/https://www.redgifs.com/watch/hotpinkacrobatickitfox
```

### `GET /gif/{ref}/download`

Download the video.

| Param | Default | Description |
|---|---|---|
| `quality` | `hd` | `hd`, `sd` (much smaller), or `silent` (HD without audio). Falls back to another quality if missing. |
| `proxy` | `false` | `false`: `302` redirect to the CDN. `true`: stream through this service with a `Content-Disposition` filename. Supports `Range`. |

```sh
curl -L -o video.mp4 'http://localhost:8080/gif/hotpinkacrobatickitfox/download'
curl -OJ 'http://localhost:8080/gif/hotpinkacrobatickitfox/download?quality=sd&proxy=true'
```

### `GET /search`

| Param | Default | Description |
|---|---|---|
| `q` | | Free-text search |
| `tags` | | Comma-separated tag names, e.g. `Amateur` |
| `order` | `trending` | `trending`, `latest`, `score`, `top`, `top7`, `top28` |
| `page`, `limit` | `1`, `20` | `limit` is 1–100; promoted slots may make the result shorter |

### `GET /health`

Returns `{"status": "ok"}`.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
```

## CI

`.github/workflows/docker.yml` builds a multi-arch image (`linux/amd64`, `linux/arm64`) and
publishes it to `ghcr.io/ricardo-duarte-av/redgifs-api`:

- push to `main` → `latest` and `sha-<commit>`
- tag `vX.Y.Z` → `X.Y.Z` and `X.Y`
- pull requests → build only, nothing is pushed

## Daily health check

`.github/workflows/healthcheck.yml` runs `scripts/healthcheck.py` against a deployed instance
every day (06:17 UTC) and on manual dispatch. If any check fails, it opens an issue labelled
`healthcheck`, or comments on the one already open. When checks pass again, it closes the issue.

Configuration (repository settings → Secrets and variables → Actions):

- secret `INSTANCE_URL`: base URL of the instance. It is only read from the secret, and the script
  scrubs it (and its hostname) from the report, so it never appears in logs or issues.
- variable `HEALTHCHECK_USER` (optional): redgifs user whose feed is used for the checks
  (default `susanna`).

Run it locally with `BASE_URL=https://your-instance python3 scripts/healthcheck.py`.
