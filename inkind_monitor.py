#!/usr/bin/env python3
"""inKind restaurant monitor for a configurable area.

inKind's full venue catalog is served from a single public, unauthenticated
endpoint (app.inkind.com/api/v5/map). This script fetches it on a schedule,
filters to venues within a radius of a center point, diffs the nearby set
against the previous run's snapshot, and emails a digest when something
changes (a venue is newly added, flagged "Leaving Soon", or has dropped off).

The reliable signal is the snapshot diff, not inKind's tags:
  - "Newly Added" (tag 32) lingers for weeks, so it is shown as context only.
  - "Leaving Soon" (tag 241) is rarely populated, so removals are caught by the
    diff (an id that was nearby last run and is gone this run).

State and config live in DATA_DIR (a persistent volume):
  - snapshot.json   : last nearby set + resolved watch ids, used for the diff
  - blacklist.json  : names/substrings to always exclude (auto-seeded)
  - watchlist.json  : names to always track (any distance), resolved to ids
  - history.jsonl   : append log of every add / leaving / remove event

Email is styled with a minimal design (Syne + IBM Plex Mono, warm white /
near-black / teal, horizontal rules, no rounded corners), keeping semantic
green / amber / red for added / leaving / removed.
"""

import html as html_lib
import json
import math
import os
import re
import smtplib
import sys
import time
import urllib.parse
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests


# ── .env loader (zero-dependency; cross-platform) ─────────────────────────────
def load_dotenv():
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Runs before the config below is read, so no shell sourcing is needed on any
    OS (Linux, macOS, Windows). It does NOT override variables already set in the
    environment, so values injected by Docker/Compose or exported in the shell
    win over the file. Tolerates blank lines, '#' comments, an optional leading
    'export ', surrounding quotes, and values containing spaces. A missing file
    is a no-op (e.g. inside the Docker image, where the env is injected directly).

    Searches, in order: $ENV_FILE, ./.env, then a .env next to this script.
    """
    override = os.environ.get("ENV_FILE")
    candidates = ([Path(override)] if override
                  else [Path(".env"), Path(__file__).resolve().parent / ".env"])
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            os.environ.setdefault(key, val)


load_dotenv()

# ── Config (env-overridable) ────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SNAPSHOT_JSON = DATA_DIR / "snapshot.json"
BLACKLIST_JSON = DATA_DIR / "blacklist.json"
WATCHLIST_JSON = DATA_DIR / "watchlist.json"
HISTORY_LOG = DATA_DIR / "history.jsonl"

MAP_URL = os.environ.get("INKIND_MAP_URL", "https://app.inkind.com/api/v5/map")

# All geo values below are env-overridable. The defaults are a worked example
# for ZIP 97204 (downtown Portland); set the INKIND_* vars in your .env for your area.
ZIP_CODE = os.environ.get("INKIND_ZIP", "97204")
# Filter anchor: a venue is included if it is within RADIUS_MI of this point.
# Example default is the centroid of 97204 (downtown Portland, west of the river).
CENTER_LAT = float(os.environ.get("INKIND_CENTER_LAT", "45.5190"))
CENTER_LNG = float(os.environ.get("INKIND_CENTER_LNG", "-122.6755"))
RADIUS_MI = float(os.environ.get("INKIND_RADIUS_MI", "2"))
AREA_LABEL = os.environ.get("INKIND_AREA_LABEL", "Downtown Portland")

# Sort/display anchor: distances shown in the email and the list ordering are
# measured from here, not from the filter centroid. Example: Pioneer Courthouse Square.
SORT_LAT = float(os.environ.get("INKIND_SORT_LAT", "45.5188"))
SORT_LNG = float(os.environ.get("INKIND_SORT_LNG", "-122.6793"))
SORT_LABEL = os.environ.get("INKIND_SORT_LABEL", "Pioneer Courthouse Square")

# Sanity floor for the catalog: a fetch that parses but returns fewer than this
# many locations is treated as a failed fetch, so a degraded/error response
# cannot read as a mass removal or overwrite the snapshot. Catalog is ~7,750
# venues today; a real platform collapse below 1,000 would be obvious news.
MIN_CATALOG = int(os.environ.get("INKIND_MIN_CATALOG", "1000"))

# inKind tag ids of interest (from the catalog's top-level tag dictionary).
TAG_NEWLY_ADDED = 32
TAG_LEAVING_SOON = 241

# Blacklist seeded the first time blacklist.json is created. Ships empty; add
# your own rules by editing blacklist.json in DATA_DIR (re-read every run).
#   exact    : full venue names to exclude (case-insensitive, exact match)
#   contains : substrings; any venue whose name contains one is excluded
DEFAULT_BLACKLIST = {
    "exact": [],
    "contains": [],
}

# Email delivery. Two backends, auto-selected in send_email():
#   - SMTP    : used when SMTP_HOST is set (works with any provider).
#   - Mailgun : used when MAILGUN_API_KEY is set and SMTP_HOST is not.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "inkind-monitor@example.com")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

# Append-only audit log of every send attempt (sent / failed / skipped), one
# JSON record per line, so "did that alert actually go out?" is answerable
# later. Defaults to email.jsonl in DATA_DIR; set EMAIL_LOG to move it
# elsewhere, or to an empty value to disable. Note the records include the
# recipient address and subject, so keep the file private.
SERVICE_NAME = "inkind"
_email_log = os.environ.get("EMAIL_LOG", str(DATA_DIR / "email.jsonl"))
EMAIL_LOG = Path(_email_log) if _email_log else None

USER_AGENT = "inkind-monitor (https://github.com/closehauled/inkind-monitor)"

# ── Design tokens (Nordic Minimal; inlined because email clients drop :root) ──
C_BG = "#f9f9f7"
C_TEXT = "#141414"
C_MUTED = "#888888"
C_ACCENT = "#006c5f"
C_BORDER = "#e0e0dc"
# Semantic green / amber / red (kept per request), harmonized with the palette.
C_GREEN = "#2f8f5f"; C_GREEN_BG = "#dff0e6"; C_GREEN_BD = "#9ed3b3"; C_GREEN_TX = "#14622f"
C_AMBER = "#c07820"; C_AMBER_BG = "#f6e8cf"; C_AMBER_BD = "#e0bd86"; C_AMBER_TX = "#7a4e12"
C_RED = "#a02020"; C_RED_BG = "#f3dada"; C_RED_BD = "#d99a9a"; C_RED_TX = "#6e1414"
# Watched (manual pin): teal outline badge on the accent color.
C_WATCH_BG = "#d4ece8"; C_WATCH_BD = "#9ccabf"; C_WATCH_TX = "#00564b"

F_SANS = "'Syne','Helvetica Neue',Arial,sans-serif"
F_MONO = "'IBM Plex Mono','SF Mono',ui-monospace,Menlo,monospace"


# ── Fetch + filter ──────────────────────────────────────────────────────────
def fetch_map():
    """Fetch and parse the full inKind venue catalog."""
    resp = requests.get(MAP_URL, timeout=60, headers={"User-Agent": USER_AGENT, "accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def haversine_mi(lat1, lng1, lat2, lng2):
    """Great-circle distance in statute miles."""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    # Clamp: float rounding can push `a` a ulp above 1.0 on garbage coordinates,
    # which would raise a math domain error in asin.
    return r * 2 * math.asin(math.sqrt(min(1.0, a)))


def load_blacklist():
    """Load the blacklist, seeding it on first run.

    Returns (exact_set, contains_list), both lowercased. Accepts either the
    current object form {"exact": [...], "contains": [...]} or a bare list
    (treated as exact names) for backward compatibility.
    """
    if not BLACKLIST_JSON.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(BLACKLIST_JSON, "w") as f:
            json.dump(DEFAULT_BLACKLIST, f, indent=2)
        print(f"  Seeded blacklist: {DEFAULT_BLACKLIST}")
        data = DEFAULT_BLACKLIST
    else:
        with open(BLACKLIST_JSON) as f:
            data = json.load(f)

    if isinstance(data, list):
        exact, contains = data, []
    else:
        exact = data.get("exact", [])
        contains = data.get("contains", [])
    exact_set = {n.strip().lower() for n in exact if n and n.strip()}
    contains_list = [s.strip().lower() for s in contains if s and s.strip()]
    return exact_set, contains_list


def is_blacklisted(name, blacklist):
    exact_set, contains_list = blacklist
    nl = name.strip().lower()
    if nl in exact_set:
        return True
    return any(sub in nl for sub in contains_list)


def maps_url(name, address, city, state):
    """Google Maps search link for a venue (hours, photos, reviews, menu)."""
    query = ", ".join(p for p in [name, address, city, state] if p)
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(query)


# Compass directionals and common street-type suffixes, abbreviated for display.
# Compound directionals must come before the single ones.
_ADDR_ABBREV = [
    ("Northeast", "NE"), ("Northwest", "NW"), ("Southeast", "SE"), ("Southwest", "SW"),
    ("North", "N"), ("South", "S"), ("East", "E"), ("West", "W"),
    ("Boulevard", "Blvd"), ("Avenue", "Ave"), ("Street", "St"), ("Place", "Pl"),
    ("Drive", "Dr"), ("Road", "Rd"), ("Lane", "Ln"), ("Court", "Ct"),
    ("Terrace", "Ter"), ("Highway", "Hwy"), ("Parkway", "Pkwy"),
    ("Circle", "Cir"), ("Square", "Sq"),
]
_ADDR_RE = [(re.compile(rf"\b{word}\b", re.IGNORECASE), abbr) for word, abbr in _ADDR_ABBREV]


def abbrev_address(address):
    """Shorten an address for display: Southeast -> SE, Boulevard -> Blvd, etc."""
    out = address or ""
    for rx, abbr in _ADDR_RE:
        out = rx.sub(abbr, out)
    return out


# Week order for closed-day reporting: inKind operating_hours days are uppercase
# full names; we display 3-letter abbreviations in Sun..Sat order.
_WEEK = [("SUNDAY", "Sun"), ("MONDAY", "Mon"), ("TUESDAY", "Tue"), ("WEDNESDAY", "Wed"),
         ("THURSDAY", "Thu"), ("FRIDAY", "Fri"), ("SATURDAY", "Sat")]


def closed_days(operating_hours):
    """From inKind operating_hours (a list of {day, time_ranges}), return the
    days the venue is closed, in week order. A day counts as open only if it
    appears with at least one non-empty time range.

      None  -> hours unknown (operating_hours missing or empty)
      []    -> open every day (no closed days)
      list  -> closed-day abbreviations, e.g. ["Mon", "Tue"]
    """
    if not operating_hours:
        return None
    open_days = {
        (entry.get("day") or "").upper()
        for entry in operating_hours
        if entry.get("time_ranges")
    }
    return [abbr for full, abbr in _WEEK if full not in open_days]


def build_venue(loc, place, dist, watched=False):
    """Normalize one catalog location into the venue record used everywhere
    (snapshot, diff, email). Single source of truth for the venue schema, shared
    by extract_nearby (radius set) and resolve_watchlist (manual pins)."""
    # Collapse internal whitespace too: a name carrying a newline could forge
    # section headers in the plain-text email part.
    name = re.sub(r"\s+", " ", loc.get("name") or "").strip()
    addr = place.get("address", "")
    city = place.get("city", "")
    state = place.get("state", "")
    tag_ids = {t.get("id") for t in (loc.get("tags") or [])}
    return {
        "location_id": loc.get("location_id"),
        "name": name,
        "address": addr,
        "city": city,
        "state": state,
        "zip": place.get("zip_code", ""),
        "link": loc.get("purchase_page_link", ""),
        "maps": maps_url(name, addr, city, state),
        "address_short": abbrev_address(addr),
        "status": loc.get("status", ""),
        "dist_mi": round(dist, 1),
        "newly_added": TAG_NEWLY_ADDED in tag_ids,
        "leaving_soon": TAG_LEAVING_SOON in tag_ids,
        "closed_days": closed_days(loc.get("operating_hours")),
        "watched": watched,
    }


def extract_nearby(catalog, blacklist):
    """Return {location_id: venue_dict} for venues within RADIUS_MI of the
    filter centroid, minus blacklist. Display distance (dist_mi) is measured from
    the sort anchor (SORT_LAT/SORT_LNG), and the list is later ordered by it."""
    nearby = {}
    for loc in catalog.get("locations", []):
        # An id-less entry would collapse onto the string key "None" and churn
        # as added/removed; skip it.
        if loc.get("location_id") is None:
            continue
        place = loc.get("location") or {}
        lat, lng = place.get("latitude"), place.get("longitude")
        if lat is None or lng is None:
            continue
        # Membership: within RADIUS_MI of the filter centroid.
        if haversine_mi(CENTER_LAT, CENTER_LNG, lat, lng) > RADIUS_MI:
            continue
        name = (loc.get("name") or "").strip()
        if is_blacklisted(name, blacklist):
            continue
        # Shown/sorted distance: from the sort anchor.
        dist = haversine_mi(SORT_LAT, SORT_LNG, lat, lng)
        nearby[str(loc.get("location_id"))] = build_venue(loc, place, dist)
    return nearby


def load_watchlist():
    """Load the manual watchlist, seeding an empty list on first run.

    The file is a bare JSON array of venue names to hand-edit, e.g.
    ["Some Cafe", "Another Bistro"]. Returns the list of non-empty
    names (original case preserved for display; matched case-insensitively)."""
    if not WATCHLIST_JSON.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(WATCHLIST_JSON, "w") as f:
            json.dump([], f, indent=2)
        print("  Seeded empty watchlist.json")
        return []
    with open(WATCHLIST_JSON) as f:
        data = json.load(f)
    if not isinstance(data, list):
        print("  Warning: watchlist.json is not a JSON array; ignoring.")
        return []
    return [n.strip() for n in data if isinstance(n, str) and n.strip()]


def resolve_watchlist(catalog, watch_names, prev_watch_ids):
    """Resolve watchlist names to catalog venues, tracking by id once matched.

    Returns (watched, watch_ids):
      watched   : {location_id: venue_dict} for every name resolved this run
                  (watched=True), regardless of distance / blacklist.
      watch_ids : {name_lower: location_id} to persist for next run.

    Resolution per name:
      - Known id (rename-safe): if we recorded an id last run and it is still in
        the catalog, reuse it even if the catalog name changed.
      - Vanished id (left platform): a previously-known id no longer in the
        catalog is emitted as nothing but kept in watch_ids as a tombstone, so
        the name is never re-matched by substring to a different venue; the
        snapshot diff (prev - curr) reports it once as removed.
      - First resolution: match by name (case-insensitive exact, else unique
        substring; ties broken by nearest to the sort anchor). No match -> log a
        typo warning and skip (never emails, so typos cannot false-alarm)."""
    by_id = {loc.get("location_id"): loc for loc in catalog.get("locations", [])}
    prev_watch_ids = prev_watch_ids or {}
    watched, watch_ids = {}, {}

    for name in watch_names:
        key = name.lower()
        loc = None
        known_id = prev_watch_ids.get(key)
        if known_id is not None:
            loc = by_id.get(known_id)
            if loc is None:
                # Recorded id has left the platform; the snapshot diff reports
                # the removal once. Keep the id as a tombstone so this name
                # never falls back to substring matching, which could silently
                # pin a different venue. To force a fresh resolution (the venue
                # returned under a new id), remove the name from watchlist.json,
                # let one run complete, then re-add it.
                print(f"  Watch '{name}': previously-resolved venue is gone from the catalog (left platform).")
                watch_ids[key] = known_id
                continue
        else:
            loc = _match_watch_name(name, catalog)
            if loc is None:
                print(f"  Watch '{name}': matched no inKind venue (typo, or not on the platform).")
                continue

        place = loc.get("location") or {}
        lat, lng = place.get("latitude"), place.get("longitude")
        dist = haversine_mi(SORT_LAT, SORT_LNG, lat, lng) if lat is not None and lng is not None else 0.0
        venue = build_venue(loc, place, dist, watched=True)
        watched[str(loc.get("location_id"))] = venue
        watch_ids[key] = loc.get("location_id")

    return watched, watch_ids


def _match_watch_name(name, catalog):
    """Find a catalog location for a watch name: case-insensitive exact match,
    else a unique substring match. Multiple candidates -> nearest to the sort
    anchor. Returns the location dict, or None."""
    nl = name.lower()
    locs = [loc for loc in catalog.get("locations", []) if loc.get("location_id") is not None]
    exact = [loc for loc in locs if (loc.get("name") or "").strip().lower() == nl]
    candidates = exact or [loc for loc in locs if nl in (loc.get("name") or "").lower()]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    def _anchor_dist(loc):
        place = loc.get("location") or {}
        lat, lng = place.get("latitude"), place.get("longitude")
        if lat is None or lng is None:
            return float("inf")
        return haversine_mi(SORT_LAT, SORT_LNG, lat, lng)

    return min(candidates, key=_anchor_dist)


# ── Snapshot state ──────────────────────────────────────────────────────────
def load_snapshot():
    if SNAPSHOT_JSON.exists():
        with open(SNAPSHOT_JSON) as f:
            return json.load(f)
    return None


def save_snapshot(nearby, watch_ids=None):
    """Write the snapshot atomically (temp file + rename), so a crash mid-write
    cannot leave a truncated snapshot.json that kills every later run."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {"last_updated": datetime.now().isoformat(), "venues": nearby,
            "watch_ids": watch_ids or {}}
    tmp = SNAPSHOT_JSON.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, SNAPSHOT_JSON)


def append_history(event, venue):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(), "event": event,
           "location_id": venue.get("location_id"), "name": venue.get("name")}
    with open(HISTORY_LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def diff(prev_venues, curr):
    """Compute added / removed / newly-leaving against the previous snapshot."""
    prev = prev_venues or {}
    # Watched (manually pinned) venues are excluded from "added": a fresh pin
    # is a manual action, not a new-to-platform venue. Their leaving-soon /
    # removed alerts still fire (those lists are not filtered).
    added = [curr[i] for i in curr if i not in prev and not curr[i].get("watched")]
    removed = [prev[i] for i in prev if i not in curr]
    newly_leaving = [
        curr[i] for i in curr
        if curr[i]["leaving_soon"] and not prev.get(i, {}).get("leaving_soon", False)
    ]
    return added, removed, newly_leaving


# ── Email: plain text ─────────────────────────────────────────────────────────
def _closed_label(v):
    """Human label for a venue's closed days: 'Closed Mon, Tue', 'Open daily',
    or 'Hours unknown'. Driven by the closed_days field set in build_venue."""
    cd = v.get("closed_days")
    if cd is None:
        return "Hours unknown"
    if not cd:
        return "Open daily"
    return "Closed " + ", ".join(cd)


def _venue_line(v, marker=""):
    addr = v.get("address_short") or abbrev_address(v.get("address", ""))
    bits = [v["name"]]
    if v.get("dist_mi") is not None:
        bits.append(f"{v['dist_mi']} mi")
    if addr:
        bits.append(addr)
    bits.append(_closed_label(v))
    line = "  - " + "  -  ".join(bits)
    if marker:
        line += f"  {marker}"
    return line


def build_text(curr, added, removed, newly_leaving, now, baseline, weekly=False):
    n = len(curr)
    all_sorted = sorted(curr.values(), key=lambda v: (v["dist_mi"], v["name"].lower()))
    head = "Weekly digest. " if weekly else ""
    L = [f"{head}Within {RADIUS_MI:g} mi of {ZIP_CODE}, distances from {SORT_LABEL}",
         f"Checked: {now}", ""]
    if baseline:
        L += ["Baseline snapshot established. Future emails report changes only.", ""]
    if added:
        L.append(f"== NEWLY ADDED ({len(added)}) ==")
        for v in sorted(added, key=lambda x: x["dist_mi"]):
            L.append(_venue_line(v))
            if v.get("maps"):
                L.append(f"      {v['maps']}")
        L.append("")
    if newly_leaving:
        L.append(f"== LEAVING SOON ({len(newly_leaving)}) ==")
        for v in sorted(newly_leaving, key=lambda x: x["dist_mi"]):
            L.append(_venue_line(v))
            if v.get("maps"):
                L.append(f"      {v['maps']}")
        L.append("")
    if removed:
        L.append(f"== REMOVED SINCE LAST CHECK ({len(removed)}) ==")
        for v in sorted(removed, key=lambda x: x["name"].lower()):
            L.append(f"  - {v['name']}  (last seen near {v.get('address','')})")
        L.append("")
    L.append("-" * 48)
    L.append(f"ALL NEARBY VENUES ({n})")
    for v in all_sorted:
        markers = []
        if v.get("watched"):
            markers.append("[WATCHED]")
        if v["leaving_soon"]:
            markers.append("[LEAVING]")
        elif v["newly_added"]:
            markers.append("[NEW]")
        L.append(_venue_line(v, " ".join(markers)))
    L.append("")
    L.append("Browse on inkind.com: https://inkind.com/locations")
    return "\n".join(L)


# ── Email: HTML (Nordic Minimal) ──────────────────────────────────────────────
def _esc(s):
    """Escape for HTML, including quotes, so a value is safe in an attribute as
    well as in element text."""
    return html_lib.escape("" if s is None else str(s), quote=True)


def _badge(label, bg, bd, tx):
    return (f'<span style="font-family:{F_MONO};font-size:10px;letter-spacing:0.08em;'
            f'text-transform:uppercase;background:{bg};color:{tx};border:1px solid {bd};'
            f'padding:2px 7px;white-space:nowrap;">{label}</span>')


def _section_label(text, color):
    return (f'<tr><td style="padding:26px 0 0;"><div style="font-family:{F_MONO};font-size:11px;'
            f'letter-spacing:0.16em;text-transform:uppercase;color:{color};'
            f'border-bottom:2px solid {color};padding-bottom:7px;">{text}</div></td></tr>')


def _highlight_row(v):
    nm = _esc(v["name"])
    if v.get("maps"):
        nm = f'<a href="{_esc(v["maps"])}" style="color:{C_TEXT};text-decoration:none;">{nm}</a>'
    addr = v.get("address_short") or abbrev_address(v.get("address", ""))
    meta_bits = []
    if v.get("dist_mi") is not None:
        meta_bits.append(f'{v["dist_mi"]} mi')
    if addr:
        meta_bits.append(_esc(addr))
    meta_bits.append(_esc(_closed_label(v)))
    meta = " &middot; ".join(meta_bits)
    return (f'<tr><td style="padding:11px 0;border-bottom:1px solid {C_BORDER};">'
            f'<div style="font-family:{F_SANS};font-weight:500;font-size:16px;color:{C_TEXT};line-height:1.3;">{nm}</div>'
            f'<div style="font-family:{F_MONO};font-size:12px;color:{C_MUTED};margin-top:3px;">{meta}</div>'
            f'</td></tr>')


def _removed_row(v):
    where = _esc(v.get("address_short") or abbrev_address(v.get("address", ""))) or _esc(v.get("city", ""))
    return (f'<tr><td style="padding:11px 0;border-bottom:1px solid {C_BORDER};">'
            f'<div style="font-family:{F_SANS};font-weight:500;font-size:16px;color:{C_TEXT};line-height:1.3;">{_esc(v["name"])}</div>'
            f'<div style="font-family:{F_MONO};font-size:12px;color:{C_MUTED};margin-top:3px;">last seen near {where}</div>'
            f'</td></tr>')


def _full_row(v):
    nm = _esc(v["name"])
    if v.get("maps"):
        nm = f'<a href="{_esc(v["maps"])}" style="color:{C_TEXT};text-decoration:none;">{nm}</a>'
    badges = []
    if v.get("watched"):
        badges.append(_badge("Watched", C_WATCH_BG, C_WATCH_BD, C_WATCH_TX))
    if v["leaving_soon"]:
        badges.append(_badge("Leaving", C_AMBER_BG, C_AMBER_BD, C_AMBER_TX))
    elif v["newly_added"]:
        badges.append(_badge("New", C_GREEN_BG, C_GREEN_BD, C_GREEN_TX))
    badge = "&nbsp;".join(badges)
    addr = v.get("address_short") or abbrev_address(v.get("address", ""))
    meta_bits = [f'{v["dist_mi"]} mi']
    if addr:
        meta_bits.append(_esc(addr))
    meta_bits.append(_esc(_closed_label(v)))
    meta = " &middot; ".join(meta_bits)
    return (
        f'<tr>'
        f'<td style="padding:10px 0;border-bottom:1px solid {C_BORDER};vertical-align:top;">'
        f'<div style="font-family:{F_SANS};font-weight:500;font-size:15px;color:{C_TEXT};line-height:1.3;">{nm}</div>'
        f'<div style="font-family:{F_MONO};font-size:12px;color:{C_MUTED};margin-top:2px;">{meta}</div>'
        f'</td>'
        f'<td style="padding:10px 0;border-bottom:1px solid {C_BORDER};vertical-align:top;text-align:right;white-space:nowrap;">{badge}</td>'
        f'</tr>'
    )


def build_html(curr, added, removed, newly_leaving, now, baseline, weekly=False):
    n = len(curr)
    all_sorted = sorted(curr.values(), key=lambda v: (v["dist_mi"], v["name"].lower()))

    # status bar (pipe-separated, top/bottom rules)
    seg = lambda txt, col: f'<span style="color:{col};">{txt}</span>'
    pipe = f'<span style="color:{C_BORDER};padding:0 9px;">|</span>'
    bar = [seg(f'{n} NEARBY', C_TEXT)]
    if added:
        bar.append(seg(f'+{len(added)} NEW', C_GREEN))
    if newly_leaving:
        bar.append(seg(f'{len(newly_leaving)} LEAVING', C_AMBER))
    if removed:
        bar.append(seg(f'-{len(removed)} GONE', C_RED))
    status_bar = pipe.join(bar)

    P = []
    P.append('<!DOCTYPE html><html><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             '<link rel="preconnect" href="https://fonts.googleapis.com">'
             '<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;500;700'
             '&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet"></head>')
    P.append(f'<body style="margin:0;padding:0;background:{C_BG};">')
    P.append(f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
             f'style="background:{C_BG};"><tr><td align="center" style="padding:28px 16px;">')
    P.append(f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
             f'style="max-width:600px;width:100%;">')

    # header
    weekly_tag = ('<span style="color:#006c5f;font-weight:500;">weekly digest</span> &middot; '
                  if weekly else '')
    P.append(f'<tr><td style="padding-bottom:16px;">'
             f'<div style="font-family:{F_MONO};font-size:12px;color:{C_MUTED};">'
             f'{weekly_tag}within {RADIUS_MI:g} mi of {ZIP_CODE} &middot; distances from {_esc(SORT_LABEL)} '
             f'&middot; checked {_esc(now)}</div>'
             f'</td></tr>')

    # status bar
    P.append(f'<tr><td style="padding:13px 0;border-top:1px solid {C_BORDER};'
             f'border-bottom:1px solid {C_BORDER};font-family:{F_MONO};font-size:13px;">'
             f'{status_bar}</td></tr>')

    if baseline:
        P.append(f'<tr><td style="padding-top:18px;font-family:{F_SANS};font-size:14px;'
                 f'color:{C_TEXT};line-height:1.5;">Baseline snapshot established. '
                 f'Future emails report changes only.</td></tr>')

    # highlight sections
    if added:
        P.append(_section_label(f'Newly added &middot; {len(added)}', C_GREEN))
        P.append('<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">')
        P += [_highlight_row(v) for v in sorted(added, key=lambda x: x["dist_mi"])]
        P.append('</table></td></tr>')
    if newly_leaving:
        P.append(_section_label(f'Leaving soon &middot; {len(newly_leaving)}', C_AMBER))
        P.append('<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">')
        P += [_highlight_row(v) for v in sorted(newly_leaving, key=lambda x: x["dist_mi"])]
        P.append('</table></td></tr>')
    if removed:
        P.append(_section_label(f'Removed since last check &middot; {len(removed)}', C_RED))
        P.append('<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">')
        P += [_removed_row(v) for v in sorted(removed, key=lambda x: x["name"].lower())]
        P.append('</table></td></tr>')

    # full list
    P.append(_section_label(f'All nearby venues &middot; {n}', C_TEXT))
    P.append('<tr><td><table role="presentation" width="100%" cellpadding="0" cellspacing="0">')
    P += [_full_row(v) for v in all_sorted]
    P.append('</table></td></tr>')

    # footer
    P.append(f'<tr><td style="padding-top:22px;font-family:{F_MONO};font-size:11px;'
             f'color:{C_MUTED};line-height:1.6;">Snapshot diff vs. previous run &middot; '
             f'daily &middot; blacklist applied &middot; '
             f'<a href="https://inkind.com/locations" style="color:{C_ACCENT};'
             f'text-decoration:none;">browse on inkind.com</a></td></tr>')

    P.append('</table></td></tr></table></body></html>')
    return "\n".join(P)


def build_email(curr, added, removed, newly_leaving, *, baseline=False, weekly=False):
    """Return (subject, text_body, html_body)."""
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    n = len(curr)
    parts = []
    if added:
        parts.append(f"{len(added)} new")
    if newly_leaving:
        parts.append(f"{len(newly_leaving)} leaving")
    if removed:
        parts.append(f"{len(removed)} gone")
    if baseline:
        subject = f"inKind changes: baseline set ({n} venues nearby)"
    elif weekly:
        summary = ", ".join(parts) if parts else "no changes"
        subject = f"inKind weekly: {summary} ({n} nearby)"
    elif parts:
        subject = "inKind changes: " + ", ".join(parts)
    else:
        subject = f"inKind changes: no changes ({n} venues nearby)"
    text = build_text(curr, added, removed, newly_leaving, now, baseline, weekly)
    html = build_html(curr, added, removed, newly_leaving, now, baseline, weekly)
    return subject, text, html


def _send_via_smtp(subject, text, html=None):
    """Send via SMTP (STARTTLS on 587, implicit SSL on 465, plain otherwise)."""
    msg = MIMEMultipart("alternative") if html else MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    if html:
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    try:
        if SMTP_PORT != 465:
            server.starttls()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
    finally:
        server.quit()


def _send_via_mailgun(subject, text, html=None):
    """Send via the Mailgun HTTPS API."""
    url = f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages"
    data = {"from": EMAIL_FROM, "to": EMAIL_TO, "subject": subject, "text": text}
    if html:
        data["html"] = html
    r = requests.post(url, auth=("api", MAILGUN_API_KEY), data=data, timeout=15)
    r.raise_for_status()


def log_email(status, subject, detail=""):
    """Append one record to the email audit log (see EMAIL_LOG above).

    Every send attempt lands here (sent / failed / skipped) so "did that alert
    actually go out?" is answerable later; provider dashboards typically retain
    only a short event history, and a missed alert is otherwise invisible.
    Never raises: a logging problem must not break an email.
    """
    if EMAIL_LOG is None:
        return
    try:
        EMAIL_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now().astimezone().isoformat(),
            "service": SERVICE_NAME,
            "status": status,
            "to": EMAIL_TO,
            "subject": subject,
            "detail": str(detail)[:300],
        }
        with open(EMAIL_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"  Warning: could not write email log: {e}")


def send_email(subject, text, html=None):
    """Send an email via SMTP or Mailgun, auto-selected by configuration.

    SMTP is used when SMTP_HOST is set (works with any provider); otherwise
    Mailgun is used when MAILGUN_API_KEY is set. If neither is configured (or
    EMAIL_TO is missing), the send is skipped with a warning.

    Returns True on success or on a deliberate skip (email not configured, e.g.
    local dev with EMAIL_TO empty), False when a send was attempted and failed,
    so callers can avoid consuming state the email should have reported.
    """
    if not EMAIL_TO:
        print("  Warning: EMAIL_TO not set, skipping email.")
        log_email("skipped", subject, "EMAIL_TO not set")
        return True
    try:
        if SMTP_HOST:
            _send_via_smtp(subject, text, html)
        elif MAILGUN_API_KEY:
            if not MAILGUN_DOMAIN:
                print("  Warning: MAILGUN_API_KEY set but MAILGUN_DOMAIN missing, skipping email.")
                log_email("skipped", subject, "MAILGUN_DOMAIN not set")
                return True
            _send_via_mailgun(subject, text, html)
        else:
            print("  Warning: no SMTP_HOST or MAILGUN_API_KEY set, skipping email.")
            log_email("skipped", subject, "no SMTP_HOST or MAILGUN_API_KEY set")
            return True
        print(f"  Email sent: {subject}")
        log_email("sent", subject)
        return True
    except Exception as e:
        print(f"  Warning: could not send email: {e}")
        log_email("failed", subject, e)
        return False


# ── Run ───────────────────────────────────────────────────────────────────────
def run(weekly=False):
    """One poll cycle: fetch, filter, diff, email, persist.

    weekly=True always sends the full digest (even with no changes), for the
    once-a-week summary; otherwise an email is sent only when something changed.
    """
    blacklist = load_blacklist()

    catalog = None
    for attempt in range(1, 4):
        print(f"Fetching inKind catalog (attempt {attempt}/3)...")
        try:
            catalog = fetch_map()
            break
        except Exception as e:
            print(f"  Failed: {e}")
            if attempt < 3:
                time.sleep(300)
    if catalog is None:
        print("All fetch attempts failed.")
        send_email(f"inKind {ZIP_CODE}: fetch failed",
                   "All 3 fetch attempts failed this run. Check container logs.")
        return

    # A 200 response that parses but carries a short/missing location list (API
    # error object, truncation, interstitial) would otherwise diff as a mass
    # removal and overwrite the snapshot. Treat it as a failed fetch.
    locs = catalog.get("locations") or []
    if len(locs) < MIN_CATALOG:
        print(f"Catalog sanity check failed: {len(locs)} locations (< {MIN_CATALOG}); treating as failed fetch.")
        send_email(f"inKind {ZIP_CODE}: degraded catalog",
                   f"Catalog returned only {len(locs)} locations (sanity floor {MIN_CATALOG}). "
                   "Snapshot left untouched; check container logs and the API response.")
        return

    nearby = extract_nearby(catalog, blacklist)
    print(f"Nearby venues (<= {RADIUS_MI:g} mi, blacklist applied): {len(nearby)}")

    snap = load_snapshot()
    prev_watch_ids = (snap or {}).get("watch_ids", {})
    watched, watch_ids = resolve_watchlist(catalog, load_watchlist(), prev_watch_ids)

    # Merge manual pins into the working set: flag a venue already nearby, or
    # pull in a far-away one. The diff / digest then treat them uniformly.
    curr = dict(nearby)
    for vid, v in watched.items():
        if vid in curr:
            curr[vid]["watched"] = True
        else:
            curr[vid] = v
    if watched:
        print(f"Watched venues resolved: {len(watched)} (total tracked: {len(curr)})")

    if snap is None:
        subject, text, html = build_email(curr, [], [], [], baseline=True)
        if send_email(subject, text, html):
            save_snapshot(curr, watch_ids)
            print("Baseline snapshot saved.")
        else:
            print("Email failed; baseline snapshot NOT saved; will re-baseline next run.")
        return

    added, removed, newly_leaving = diff(snap.get("venues", {}), curr)
    for v in added:
        append_history("added", v)
    for v in removed:
        append_history("removed", v)
    for v in newly_leaving:
        append_history("leaving_soon", v)

    sent = True
    if weekly:
        print(f"Weekly digest: {len(added)} added, {len(newly_leaving)} leaving, {len(removed)} removed.")
        subject, text, html = build_email(curr, added, removed, newly_leaving, weekly=True)
        sent = send_email(subject, text, html)
    elif added or removed or newly_leaving:
        print(f"Changes: {len(added)} added, {len(newly_leaving)} leaving, {len(removed)} removed.")
        subject, text, html = build_email(curr, added, removed, newly_leaving)
        sent = send_email(subject, text, html)
    else:
        print("No changes since last check; no email sent.")

    if sent:
        save_snapshot(curr, watch_ids)
    else:
        # history.jsonl already logged these events and will log them again on
        # the retry run; accepted, it is an append log. The snapshot must not
        # advance past a diff that was never delivered.
        print("Email failed; snapshot NOT saved; changes will be re-reported next run.")


def run_test():
    """Send sample emails (real nearby data) so the format can be reviewed.

    Does NOT touch the persistent snapshot, so it is safe to run repeatedly.
    Fabricates plausible added / leaving / removed entries from the real
    nearby set so every section of the layout is populated.
    """
    blacklist = load_blacklist()
    catalog = fetch_map()
    curr = extract_nearby(catalog, blacklist)
    print(f"TEST: {len(curr)} nearby venues fetched.")
    venues = sorted(curr.values(), key=lambda v: v["dist_mi"])

    # 1) Baseline-style email (full list, no changes).
    s, t, h = build_email(curr, [], [], [], baseline=True)
    send_email("[TEST 1/3] " + s, t, h)

    # 2) Changes email: take a few real nearby venues and mark them as
    #    added / leaving / removed so every section gets rendered.
    sample = venues[:6]
    fake_added = sample[0:2]
    fake_leaving = [dict(v, leaving_soon=True) for v in sample[2:4]]
    fake_removed = sample[4:6]
    curr2 = dict(curr)
    for v in fake_leaving:
        curr2[str(v["location_id"])] = v
    # Mark a couple of venues as watched so the [WATCHED] marker / teal badge
    # renders, including one stacked with [LEAVING].
    for v in (sample[0], fake_leaving[0]):
        curr2[str(v["location_id"])] = dict(curr2[str(v["location_id"])], watched=True)
    s, t, h = build_email(curr2, fake_added, fake_removed, fake_leaving)
    send_email("[TEST 2/3] " + s, t, h)

    # 3) Weekly digest (full list, marked as the weekly summary).
    s, t, h = build_email(curr, [], [], [], weekly=True)
    send_email("[TEST 3/3] " + s, t, h)
    print("TEST emails sent.")


def run_search(term):
    """Print catalog venues whose name contains `term` (case-insensitive), so a
    watchlist name can be confirmed against the real inKind roster. Read-only."""
    catalog = fetch_map()
    tl = term.lower()
    hits = [loc for loc in catalog.get("locations", [])
            if tl in (loc.get("name") or "").lower()]
    hits.sort(key=lambda loc: (loc.get("name") or "").lower())
    print(f"{len(hits)} match(es) for '{term}':")
    for loc in hits:
        place = loc.get("location") or {}
        where = ", ".join(p for p in [place.get("city", ""), place.get("state", "")] if p)
        print(f"  {loc.get('location_id')}  {(loc.get('name') or '').strip()}  ({where})")


def _guarded_run(weekly=False):
    """Run one cycle; on any unexpected crash, alert by email and exit nonzero.

    Without this, a crash after the fetch (malformed hand-edited watchlist /
    blacklist JSON, a corrupted snapshot, catalog field drift) is visible only
    in the container logs, and a dead monitor looks exactly like a quiet one:
    the normal steady state is "no changes, no email".
    """
    try:
        run(weekly=weekly)
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        send_email(f"inKind {ZIP_CODE}: monitor crashed",
                   "The scheduled run crashed:\n\n" + tb)
        sys.exit(1)


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_test()
    elif "--search" in sys.argv:
        i = sys.argv.index("--search")
        term = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        if not term:
            print("Usage: inkind_monitor.py --search \"<name fragment>\"")
            sys.exit(2)
        run_search(term)
    elif "--weekly" in sys.argv:
        _guarded_run(weekly=True)
    else:
        _guarded_run()
