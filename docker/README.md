# Docker deployment — bgutil sidecar + downloader

This directory ships a 2-container deployment:

| Service     | Image                                               | Purpose                                             |
| ----------- | --------------------------------------------------- | --------------------------------------------------- |
| `bgutil`    | `brainicism/bgutil-ytdlp-pot-provider:latest`       | PO Token provider (Node.js sidecar).                |
| `downloader`| built from `Dockerfile.downloader`                  | `python main.py --bot` with yt-dlp + bgutil plugin. |

The two talk over an internal `ytdlp` bridge network. Port `4416` is
**not** published to the host in the production variant, so the provider
is reachable only from the downloader container.

## Full headless deployment

```bash
# Prepare volume dirs on the host.
mkdir -p docker/data/downloads docker/data/logs   # media files / per-download run logs
touch docker/data/cookies.txt   # optional, ignored unless YTDLP_AUTH_METHOD=cookie-file

# Build + start.
docker compose -f docker/docker-compose.yml up -d --build

# The bot needs a Telegram token (from @BotFather) and the chat ids it may
# serve. Compose reads them from docker/.env (or your shell) and refuses to
# start without them:
#   YTDLP_BOT_TOKEN=123456:ABC...
#   YTDLP_BOT_ALLOWED_CHATS=123456789
# Do not know your chat id yet? Put 0 there, start the bot and message it:
# it answers a chat that is not listed with that chat's id.

# Watch the bot boot: it logs its BotConfig (the token is never printed),
# connects to Telegram and waits for links.
docker compose -f docker/docker-compose.yml logs -f downloader
```

### Environment variables (downloader)

Put the optional ones in `docker/.env` (or export them) next to the token: compose passes
each one in the table on to the container, an empty one means the bot's default. A variable
that is not listed in `docker-compose.yml` never reaches the bot, so add a line there when
you add a new setting (`tests/test_docker_compose_env.py` checks the table against the file).
After a change, `docker compose -f docker/docker-compose.yml up -d` recreates the container.

| Var                                | Default                           | Notes                                                  |
| ---------------------------------- | --------------------------------- | ------------------------------------------------------ |
| `YTDLP_AUTH_METHOD`                | `bgutil`                          | `bgutil` / `cookie-file` / `none`.                     |
| `YTDLP_GETPOT_BGUTIL_BASEURL`      | `http://bgutil:4416`              | Sidecar address. Read by the bot and passed to yt-dlp as `--extractor-args youtubepot-bgutilhttp:base_url=...` (the plugin itself reads no env var). |
| `YTDLP_COOKIE_FILE`                | `cookies.txt`                     | Only consulted when `YTDLP_AUTH_METHOD=cookie-file`.   |
| `YTDLP_OUTPUT_DIR`                 | `/data/downloads`                 | Where media files are written.                         |
| `YTDLP_FRAGMENT_RETRIES`           | `10`                              | Live recordings: retries per fragment. `0` = none, `infinite` = forever (then only the stall watchdog ends a run). Anything else is refused at start-up. |
| `YTDLP_STALL_MINUTES`              | `5`                               | Live recordings: stop a recording that gets no new data for this many minutes. `0` = never (up to `1440`). Anything else is refused at start-up. |
| `YTDLP_BOT_TOKEN`                  | *(required)*                      | Telegram bot token from @BotFather. Keep it out of git and out of the compose file. |
| `YTDLP_BOT_ALLOWED_CHATS`          | *(required)*                      | Comma-separated chat ids the bot serves (a group's id is negative). Anyone else is told their id once and ignored. |
| `YTDLP_BOT_ALLOWED_HOSTS`          | YouTube                           | Hosts a link may point to (sub-domains match), `*` for any. The default is `youtube.com`, `youtu.be`, `youtube-nocookie.com`. |
| `YTDLP_BOT_MAX_PARALLEL`           | `2`                               | Downloads at once, `1`–`5`.                            |
| `YTDLP_BOT_MAX_QUEUED`             | `20`                              | Downloads that may wait for a slot, `1`–`100`.         |
| `YTDLP_AUDIO_LANG`                 | `original`                        | Audio track of a dubbed video: the original by default; a language code such as `ko` picks that language first (for a foreign video, YouTube's auto-dub). |

## Local-dev variant: only the sidecar

On a Windows / macOS dev box you usually want to keep Python running on
the host (so you can edit and debug) but still have a real bgutil
provider. In that case run only the sidecar and **publish** port 4416:

```bash
docker run -d --rm --name bgutil -p 4416:4416 \
    brainicism/bgutil-ytdlp-pot-provider:latest

# On the host (the CLI asks for the provider URL; Enter keeps the
# default http://127.0.0.1:4416):
python main.py --cli                                     # menu option 6
```

The GUI has the same setting under 인증 방식 → PO토큰(bgutil) → Provider URL.
The `YTDLP_*` variables above are read only by the bot (`--bot`).

To tear down:

```bash
docker stop bgutil
```

## Sanity check

Syntax-validate the compose file without bringing anything up:

```bash
docker compose -f docker/docker-compose.yml config
```

Ping the provider directly (local-dev variant only, needs published port):

```bash
curl http://127.0.0.1:4416/ping
```

## Notes

- The downloader image installs `yt-dlp[default]>=2025.11.12`. The
  `[default]` extra pulls in `yt-dlp-ejs` (the JS challenge solver
  scripts, pinned to the matching yt-dlp version), and the image copies
  `deno` 2.9.7 from `denoland/deno:bin-2.9.7` to run them. Bump the deno
  tag deliberately when yt-dlp starts requiring a newer runtime.
- `bgutil-ytdlp-pot-provider` is installed into the downloader image at
  build time. This is a yt-dlp plugin, not a runtime dep; it is never
  imported from the host-side Python in development.
- The compose file mounts `./data/cookies.txt` read-only so that
  switching to `YTDLP_AUTH_METHOD=cookie-file` works without rebuilding.
