# inKind monitor

Watches [inKind](https://inkind.com)'s restaurant roster around a location you
choose and emails a digest when a nearby venue is **newly added**, flagged
**leaving soon**, or has **dropped off** the platform.

Single-file Python, one dependency (`requests`), runs on Linux, macOS, and
Windows, with or without Docker. Email goes out via plain SMTP or the Mailgun
API.

## How it works

inKind serves its entire venue catalog from one public, unauthenticated
endpoint: [https://app.inkind.com/api/v5/map](https://app.inkind.com/api/v5/map)
(~41 MB JSON, thousands of venues). Each run:

1. Fetches the catalog and filters to venues within `INKIND_RADIUS_MI` of your
   center point.
2. Applies the name blacklist (`<data>/blacklist.json`).
3. Diffs the nearby set against the previous run's snapshot
   (`<data>/snapshot.json`).
4. Emails a digest if anything changed.

Membership is decided by distance from the filter centroid, but the distances
shown in the email and the list ordering are measured from a separate sort
anchor (`INKIND_SORT_*`), so you can, for example, include everything within
2 mi of a ZIP centroid yet order the list by how close it is to your home.

The **snapshot diff** is the reliable signal. inKind's own tags are used only as
context, because:

- `Newly Added` (tag 32) lingers on venues for weeks, so it is not a "new since
  last check" signal.
- `Leaving Soon` (tag 241) is rarely populated, so removals are caught by an id
  disappearing from the nearby set, not by the tag.

## Configuration

Copy `.env.example` to `.env` and edit. All values are environment variables.

### Email backend (pick one)

If `SMTP_HOST` is set, email is sent via SMTP; otherwise, if `MAILGUN_API_KEY`
is set, it is sent via the Mailgun API. If neither is set the run still works,
it just skips the email with a warning.

| Var | Default | Meaning |
|-----|---------|---------|
| `EMAIL_TO` | (none, required) | Recipient address |
| `EMAIL_FROM` | `inkind-monitor@example.com` | Sender address |
| `SMTP_HOST` | (empty) | SMTP server; set this to use SMTP |
| `SMTP_PORT` | `587` | `587` = STARTTLS, `465` = implicit SSL |
| `SMTP_USER` | (empty) | SMTP username (omit for unauthenticated relays) |
| `SMTP_PASS` | (empty) | SMTP password / app password |
| `MAILGUN_API_KEY` | (empty) | Mailgun API key; used when `SMTP_HOST` is empty |
| `MAILGUN_DOMAIN` | (empty) | Mailgun sending domain (required for Mailgun) |

### Area of interest

The defaults are a worked example for ZIP 97204 (downtown Portland). Change them
for your area.

| Var | Default | Meaning |
|-----|---------|---------|
| `INKIND_ZIP` | `97204` | Label used in the subject/body |
| `INKIND_CENTER_LAT` | `45.5190` | Filter centroid latitude |
| `INKIND_CENTER_LNG` | `-122.6755` | Filter centroid longitude |
| `INKIND_RADIUS_MI` | `2` | Membership radius in miles |
| `INKIND_AREA_LABEL` | `Downtown Portland` | Human label |
| `INKIND_SORT_LAT` | `45.5188` | Sort/display anchor latitude |
| `INKIND_SORT_LNG` | `-122.6793` | Sort/display anchor longitude |
| `INKIND_SORT_LABEL` | `Pioneer Courthouse Square` | Sort anchor label shown in the email |

### Schedule and timezone

| Var | Default | Meaning |
|-----|---------|---------|
| `TZ` | `UTC` | Timezone the schedule fires in (e.g. `America/Los_Angeles`) |
| `INKIND_CRON_DAILY` | `0 9 * * 0,2-6` | Change-check cron (every day except Mon, 09:00) |
| `INKIND_CRON_WEEKLY` | `0 9 * * 1` | Weekly full-digest cron (Mon 09:00) |

The weekly run always emails the complete nearby list (not just changes); the
daily run only emails when something changed. One run on container start
establishes the baseline snapshot.

The Docker image bundles `tzdata`, so `TZ` is honored. If you run the script
directly under cron, the schedule simply follows the host's local time.

## Blacklist

`<data>/blacklist.json` controls which venues are hidden. It ships empty and is
auto-created on first run. Two rule types, both case-insensitive:

- `exact`: full venue names to drop (exact match)
- `contains`: substrings; any venue whose name contains one is dropped

```json
{
  "exact": ["Some Specific Place"],
  "contains": ["bbq", "chicken"]
}
```

Edit it in the data directory to add or remove entries; no rebuild needed, it is
re-read every run. (A bare JSON array is also accepted and treated as `exact`
names.)

## Email layout

Highlights first (newly added, then leaving soon, then removed). The **full
nearby list** is at the bottom, sorted by distance from the sort anchor, with
`[NEW]` / `[LEAVING]` markers inline. Each venue name links to a Google Maps
search (hours, photos, reviews, menu), since inKind has no useful per-venue
detail page. Both plain-text and HTML parts are sent.

## State files

Written to the data directory (`/data` in Docker, `DATA_DIR` standalone):

- `snapshot.json` - last nearby set, used for the diff
- `blacklist.json` - excluded venue names
- `history.jsonl` - append log of every add / leaving / remove event

## Run with Docker

```
cp .env.example .env
# edit .env
docker compose up --build -d
```

## Run without Docker

Pure Python (3.8+), no system tools required, so it runs on Linux, macOS, and
Windows. The script auto-loads a `.env` file from the current directory (it does
not override variables already set in the environment), so no shell sourcing is
needed.

`DATA_DIR` must point at a writable directory. The default `/data` suits the
Docker volume, not a normal user account, so for standalone use set it to a
local path. Do not leave `DATA_DIR` in `.env` if you later switch to Docker,
since the container expects the default `/data` volume.

Linux / macOS:

```
pip install -r requirements.txt
cp .env.example .env          # then edit
DATA_DIR=./data python3 inkind_monitor.py
```

Windows (PowerShell):

```
pip install -r requirements.txt
copy .env.example .env        # then edit, and add a line: DATA_DIR=./data
python inkind_monitor.py
```

### Scheduling (standalone)

The first run sets the baseline; later runs email only on changes, and
`--weekly` always sends the full digest.

Linux / macOS, with cron:

```
0 9 * * 0,2-6  cd /path/to/inkind && DATA_DIR=./data python3 inkind_monitor.py
0 9 * * 1      cd /path/to/inkind && DATA_DIR=./data python3 inkind_monitor.py --weekly
```

Windows, with Task Scheduler: create two tasks running `python inkind_monitor.py`
(daily, e.g. every day except Monday) and `python inkind_monitor.py --weekly`
(weekly, Monday). Set each task's "Start in" to the repo folder so `.env` and the
data directory resolve, and add `DATA_DIR=./data` to your `.env`.

## Test the email format

Sends three sample emails (real nearby data, fabricated change sections)
without touching the snapshot:

```
DATA_DIR=./data python3 inkind_monitor.py --test
```

or, with Docker:

```
docker compose run --rm inkind python3 /app/inkind_monitor.py --test
```

## License

MIT. See [LICENSE](LICENSE).
