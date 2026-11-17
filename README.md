# Shiftboard

A small self-hosted shift calendar for your homelab. Set up your shift types once, then tap days to fill in your roster (like Super Shift), add on-call periods, and share a read-only link or calendar feed so others can see when you're working.

## Quick start

Every push to `main` builds `ghcr.io/apevx/shiftboard:latest` (amd64 + arm64) via GitHub Actions.

1. Paste `docker-compose.yml` into Dockge (or run `docker compose up -d`), changing `ADMIN_PASSWORD` first.
2. The image is private by default: either run `docker login ghcr.io -u Apevx` on the host once with a token that has `read:packages`, or make the package public under GitHub → Packages → shiftboard → Package settings.

To build locally instead, replace the `image:` line with `build: .`.

Then open `http://<homelab-ip>:8085`. Data is stored in `./data/shiftboard.db` (SQLite), so back up that folder.

## Using it

- **Paint shifts:** pick a shift in the bottom bar, then tap days. Tap a day again to remove the shift. **Erase** clears a day. Turn on **2× Stack** to put two shifts on one day (e.g. a double).
- **View mode:** tap a day to see times, set or change its shift, see on-call, and add a private note.
- **On call:** pick **On call**, tap the first day, then tap the last day. Set the times (it remembers your last ones, e.g. 17:00 → 08:00) and save. It shows as an amber bar under each day it covers. You can also add on-call from the day sheet.
- **Rotation (Settings → Rotation):** build a repeating pattern such as `D D N N – – – –` and apply it to a date range. You can still tweak individual days afterwards.
- **Shifts (Settings → Shifts):** add or edit shift types: name, short code, colour, start/end time (overnight is handled), or all-day for leave/training. Deleting a shift that's already on the calendar only hides it, so your history stays.
- The header shows totals for the month: shift count, hours, on-call hours and a count for each shift type.
- Swipe left/right (or use the arrow keys) to change month.

## Sharing

Settings → Share gives you two links. Both are read-only, and your private notes are never included.

| Link | Use |
|---|---|
| `/s/<token>` | Web page view for family or friends |
| `/s/<token>/calendar.ics` | Calendar subscription for Apple/Google/Outlook calendar, or Home Assistant's *Remote Calendar* integration |

**Reset links** creates a new token, and the old links stop working.

### Making it reachable outside your network

The share links only work on your LAN until you expose the app. **Google Calendar fetches feeds from Google's own servers**, so the `.ics` URL has to be reachable from the internet. Options:

- **Cloudflare Tunnel:** point a hostname (e.g. `shifts.example.com`) at `http://shiftboard:8080`. You can add a Cloudflare Access rule on `/` while leaving `/s/*` public.
- **Tailscale Funnel**, or a reverse proxy (Caddy, Nginx Proxy Manager, Traefik) with HTTPS.

Then set `PUBLIC_URL: https://shifts.example.com` in `docker-compose.yml` so the Share tab gives out the public links.

Editing is protected by `ADMIN_PASSWORD`. If you expose the whole app, use a long password; for extra safety, only expose the `/s/` paths.

## Config (environment variables)

| Var | Default | |
|---|---|---|
| `ADMIN_PASSWORD` | *(empty = no login)* | Password for editing |
| `TZ` | `Australia/Sydney` | Timezone used for the calendar feed |
| `PUBLIC_URL` | *(blank)* | External base URL used in share links |
| `APP_TITLE` | `My Shifts` | Initial calendar name (can be changed later in Settings) |
| `DB_PATH` | `/data/shiftboard.db` | |

## Tech

FastAPI + SQLite + one vanilla-JS page, with no build step. The API lives under `/api/*`; see `app/main.py`.
