#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xfce-claude-quota — show your official Claude usage limits in the XFCE panel.

WHY THIS DESIGN
---------------
On XFCE 4.20 there are no Python bindings for libxfce4panel (only Libxfce4ui /
Libxfce4util ship a GIR), so a *native* panel plugin cannot be written in Python.
The documented, supported way to put a custom widget in the panel is the
**Generic Monitor (genmon)** plugin, which runs a command on an interval and
renders its output. This script is that command.

DATA SOURCES
------------
1. Official usage percentages — the same numbers the VS Code "Account & usage"
   popup shows (Session 5hr, Weekly, and any per-model weekly buckets, with
   reset times), read from the response's structured `limits` array.
   These come from Anthropic's OAuth usage endpoint, authenticated with the
   access token Claude Code already stores in ~/.claude/.credentials.json.
   No separate API key exists or is needed; Claude Code refreshes the token.
   Responses are cached so the panel never hits the network more than once
   per `api_refresh_seconds` (default 120), regardless of the genmon period.

2. Local history — Claude Code writes a transcript of every message under
   ~/.claude/projects/**/*.jsonl (tokens, model, timestamps). The detailed
   window uses it for the usage-over-time graph and the per-message table.

3. Platform status — the public Statuspage roll-up at status.claude.com
   (/api/v2/status.json), an unauthenticated static JSON file on a CDN. When
   Claude itself is degraded the panel image gains a status-coloured frame and
   the tooltip leads with the status description; the detailed window grows a
   click-through banner. Cached like the usage call.

The panel image adapts to the panel it lives on: horizontal bars stacked on a
normal panel, upright columns once the panel is tall enough that horizontal
ones would be blobs (see use_vertical_bars).

MODES
-----
  (no args)               genmon mode: fetch/refresh data, render the panel
                          image, print genmon XML.
  --detail                open the detailed window (gauges + graph + table).
                          Single-instance: a second launch focuses the first.
  --config                open the Preferences dialog.
  --self-test             print official gauges + local stats (no GUI).
  --reindex               rebuild the local-history cache, then run --self-test.
  --print-genmon-command  print the exact command to paste into genmon.

Author: robert.bullock@gmail.com
License: MIT
"""

import os
import sys
import json
import glob
import math
import time
import tempfile
import argparse
import sqlite3
import configparser
import urllib.request
import urllib.error
import subprocess
import email.utils
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

APP_ID = "xfce-claude-quota"
DBUS_APP_ID = "org.xfce.ClaudeQuota"
SCRIPT_PATH = os.path.abspath(__file__)

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), APP_ID
)
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), APP_ID
)
DB_FILE = os.path.join(CACHE_DIR, "usage.db")
PANEL_PNG = os.path.join(CACHE_DIR, "panel.png")
USAGE_CACHE = os.path.join(CACHE_DIR, "official-usage.json")
STATUS_CACHE = os.path.join(CACHE_DIR, "platform-status.json")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
MANAGE_URL = "https://claude.ai/settings/usage"

# Claude's public Statuspage. /api/v2/status.json is the documented "roll-up"
# endpoint: a ~200-byte static JSON file served from a CDN, no auth, no
# rate-limit worth worrying about. We deliberately do not fetch summary.json /
# components.json — the panel only needs the single overall indicator.
STATUS_URL = "https://status.claude.com/api/v2/status.json"
STATUS_PAGE_URL = "https://status.claude.com"

# The usage endpoint is undocumented and Cloudflare-fronted: it publishes no
# rate-limit headers, so on a 429 we back off adaptively. These bound the
# exponential backoff used when the 429 carries no Retry-After header.
BACKOFF_BASE = 120     # first 429 wait, seconds
BACKOFF_CAP = 900      # longest backoff between attempts, seconds (15 min)
RETRY_AFTER_CAP = 3600  # never trust a Retry-After longer than this

DEFAULTS = {
    "claude_dir": os.path.expanduser("~/.claude"),
    "api_refresh_seconds": "120",  # min seconds between calls to the usage API
    "status_refresh_seconds": "300",  # min seconds between status.claude.com hits
    "panel_font": "auto",         # Pango font for the numbers, or "auto"
    "panel_text_color": "auto",   # "auto"/"dark"/"light"/"#rrggbb" for numbers
    "panel_size": "auto",         # rendered image height in px, or "auto"
    "panel_width": "auto",        # rendered image width in px, or "auto"
    "bar_color_mode": "pace",     # "pace" (burn rate vs. clock) or "absolute"
    "panel_bar_layout": "auto",   # "auto"/"horizontal"/"vertical" panel bars
    # Buckets that get a panel bar, in order; missing ones are skipped.
    "panel_gauges": "five_hour,seven_day,seven_day_fable",
}

# Known usage buckets, in display order. Unknown non-null buckets with a
# numeric utilization are appended after these with a prettified key.
BUCKET_LABELS = [
    ("five_hour", "Session (5hr)"),
    ("seven_day", "Weekly (7 day)"),
    ("seven_day_sonnet", "Weekly Sonnet"),
    ("seven_day_opus", "Weekly Opus"),
    ("seven_day_oauth_apps", "Weekly (OAuth apps)"),
]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    cfg = configparser.ConfigParser()
    cfg["general"] = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            cfg.read(CONFIG_FILE)
        except configparser.Error:
            pass  # fall back to defaults on a corrupt file
    for k, v in DEFAULTS.items():
        if not cfg.has_option("general", k):
            cfg.set("general", k, v)
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as fh:
        cfg.write(fh)


def cfg_get(cfg, key):
    return cfg.get("general", key, fallback=DEFAULTS.get(key, ""))


def cfg_int(cfg, key, default):
    try:
        return int(float(cfg_get(cfg, key)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Official usage (OAuth endpoint, cached)
# ---------------------------------------------------------------------------

def read_access_token(cfg):
    """Return (token, error). Reads Claude Code's own OAuth credentials."""
    path = os.path.join(os.path.expanduser(cfg_get(cfg, "claude_dir")),
                        ".credentials.json")
    try:
        with open(path) as fh:
            creds = json.load(fh)
    except OSError:
        return None, "credentials not found: {}".format(path)
    except ValueError:
        return None, "credentials file is not valid JSON"
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return None, "no accessToken in credentials (log in via Claude Code)"
    return token, None


def read_subscription_type(cfg):
    path = os.path.join(os.path.expanduser(cfg_get(cfg, "claude_dir")),
                        ".credentials.json")
    try:
        with open(path) as fh:
            return (json.load(fh).get("claudeAiOauth") or {}).get("subscriptionType")
    except (OSError, ValueError):
        return None


def _atomic_write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _parse_retry_after(value, now):
    """Parse a Retry-After header (delta-seconds or HTTP-date) to an epoch, or
    None if absent/unparseable."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return now + int(value)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fmt_secs(secs):
    secs = int(round(secs))
    if secs < 90:
        return "{}s".format(max(1, secs))
    minutes = secs // 60
    if minutes < 90:
        return "{}m".format(minutes)
    return "{}h {}m".format(minutes // 60, minutes % 60)


def fetch_official_usage(cfg, force=False):
    """Return (data, fetched_at, stale, error).

    A small JSON cache gates network access so the endpoint is hit at most once
    per api_refresh_seconds even if genmon polls faster. The endpoint is an
    undocumented, Cloudflare-fronted internal endpoint with no rate-limit
    headers, so on a 429 we back off adaptively — honouring Retry-After if it is
    present, otherwise growing an exponential backoff (capped). That backoff is
    persisted in the cache file, so the panel and the detail window share one
    schedule and never compound the request rate. force=True (manual refresh)
    skips the freshness gate but still respects an active backoff window — we
    cannot beat a rate limit by asking harder.
    """
    refresh = max(30, cfg_int(cfg, "api_refresh_seconds", 120))
    now = time.time()

    cached = None
    try:
        with open(USAGE_CACHE) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        pass

    def cached_get(key, default=None):
        return cached.get(key, default) if cached else default

    def stale():
        return cached_get("data"), cached_get("fetched_at"), True, cached_get("error")

    if cached:
        if not force and now - cached_get("fetched_at", 0) < refresh:
            return cached_get("data"), cached_get("fetched_at"), False, None
        if now < cached_get("next_earliest", 0):
            return stale()  # in a backoff window; don't touch the network

    token, err = read_access_token(cfg)
    next_earliest = now + refresh
    backoff = cached_get("backoff", 0)

    if token:
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": "Bearer " + token,
            "anthropic-beta": OAUTH_BETA,
            "Content-Type": "application/json",
            "User-Agent": APP_ID,
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            _atomic_write_json(USAGE_CACHE, {
                "fetched_at": now, "last_attempt": now, "data": data,
                "error": None, "next_earliest": 0, "backoff": 0,
            })
            return data, now, False, None
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                # This endpoint sends "Retry-After: 0" (no useful guidance), so
                # only honour a *positive* delta; otherwise grow the backoff.
                ra = _parse_retry_after(exc.headers.get("Retry-After"), now)
                if ra is not None and ra - now >= 1:
                    wait = max(refresh, min(ra - now, RETRY_AFTER_CAP))
                else:
                    backoff = min(max(backoff * 2, BACKOFF_BASE), BACKOFF_CAP)
                    wait = backoff
                next_earliest = now + wait
                err = "rate-limited (429); retry in {}".format(_fmt_secs(wait))
            elif exc.code in (401, 403):
                err = ("auth expired — run Claude Code once to refresh the "
                       "token (HTTP {})".format(exc.code))
                backoff = 0
            else:
                err = "usage API returned HTTP {}".format(exc.code)
                backoff = 0
        except (urllib.error.URLError, OSError, ValueError) as exc:
            err = "network error: {}".format(getattr(exc, "reason", exc))
            next_earliest = now + min(refresh, 60)

    _atomic_write_json(USAGE_CACHE, {
        "fetched_at": cached_get("fetched_at", 0),
        "last_attempt": now,
        "data": cached_get("data"),
        "error": err,
        "next_earliest": next_earliest,
        "backoff": backoff,
    })
    return cached_get("data"), cached_get("fetched_at"), True, err


# The usage endpoint describes the same limits twice: as a flat map of bucket
# keys, and as a structured `limits` array. We prefer the array — it is
# self-describing, it carries the per-model weekly buckets that the flat keys
# no longer populate (seven_day_sonnet / seven_day_opus now arrive null while
# the scoped entry is live), and it doesn't need an allowlist to keep the
# response's unlaunched codename buckets (nimbus_quill, cinder_cove, tangelo,
# juniper_tide, …) out of the display. The flat map stays as the fallback.

# `group` is the coarse bucket family, and the only thing that tells us how
# long a window runs — needed to turn `resets_at` into "how far through are we".
LIMIT_GROUP_SECONDS = {"session": 5 * 3600, "weekly": 7 * 86400}
# Session gauges first, then weekly; unknown families sort last.
LIMIT_GROUP_ORDER = {"session": 0, "weekly": 1}
# Base key/label per family, for scoped entries and unrecognised kinds. The
# keys deliberately match the legacy flat ones, so a scoped Opus bucket lands
# on `seven_day_opus` either way.
LIMIT_GROUP_NAMES = {"session": ("five_hour", "Session"),
                     "weekly": ("seven_day", "Weekly")}
# The two headline kinds keep their established keys — the default
# `panel_gauges` selects the panel's bars by `five_hour` / `seven_day`.
LIMIT_KIND_IDENTITY = {
    "session": ("five_hour", "Session (5hr)"),
    "weekly_all": ("seven_day", "Weekly (7 day)"),
}


def _slug(text):
    """'Weekly Opus' -> 'weekly_opus'; '' for anything with no word characters."""
    chars = "".join(c if c.isalnum() else "_" for c in (text or "").lower())
    return "_".join(part for part in chars.split("_") if part)


def _scope_names(scope):
    """Display names out of a limit's `scope`, e.g. ['Fable'] or ['Opus', 'Cowork'].

    Each member is a dict with a display_name today, but tolerate a bare string
    so a future shape doesn't drop the gauge.
    """
    names = []
    if not isinstance(scope, dict):
        return names
    for part in ("model", "surface"):
        val = scope.get(part)
        if isinstance(val, dict):
            val = val.get("display_name") or val.get("id")
        if isinstance(val, str) and val.strip():
            names.append(val.strip())
    return names


def _limit_identity(kind, group, names):
    """(key, label) for one limit entry."""
    if not names and kind in LIMIT_KIND_IDENTITY:
        return LIMIT_KIND_IDENTITY[kind]
    base_key, base_label = LIMIT_GROUP_NAMES.get(
        group, (_slug(kind) or "limit", (kind or "limit").replace("_", " ").title()))
    if not names:
        # A kind we don't recognise, in a family we do: name it after the kind
        # rather than inventing a label the API never used.
        return (_slug(kind) or base_key,
                (kind or base_label).replace("_", " ").title())
    return ("_".join([base_key] + [_slug(n) for n in names]),
            "{} {}".format(base_label, " · ".join(names)))


def _gauges_from_limits(limits):
    """Gauge dicts from the structured `limits` array; [] if it yields none."""
    rows = []
    for index, lim in enumerate(limits):
        if not isinstance(lim, dict):
            continue
        pct = lim.get("percent")
        if not isinstance(pct, (int, float)):
            continue
        kind, group = lim.get("kind") or "", lim.get("group") or ""
        names = _scope_names(lim.get("scope"))
        key, label = _limit_identity(kind, group, names)
        rows.append(((LIMIT_GROUP_ORDER.get(group, 9), 1 if names else 0, index), {
            "key": key,
            "label": label,
            "utilization": float(pct),
            "resets_at": lim.get("resets_at"),
            "window": LIMIT_GROUP_SECONDS.get(group),
            "group": group,
            # Carried through but not acted on: `severity` is the API's own
            # coarse verdict (we colour by burn rate instead), and the meaning
            # of `is_active` isn't documented — it is false here for a weekly
            # bucket that is plainly in force, so don't filter on it.
            "severity": lim.get("severity"),
            "is_active": lim.get("is_active"),
        }))
    rows.sort(key=lambda row: row[0])

    gauges, used_keys = [], {}
    for _sort, gauge in rows:
        key = gauge["key"]
        if key in used_keys:
            used_keys[key] += 1
            gauge["key"] = "{}_{}".format(key, used_keys[key])
        else:
            used_keys[key] = 1
        gauges.append(gauge)
    return gauges


def panel_gauges(cfg, gauges):
    """The gauges that get a panel bar, in configured order.

    Keys are the ones extract_gauges produces. Anything the current response
    doesn't carry is skipped rather than drawn empty, so a machine with no
    Fable usage simply gets two bars instead of three.
    """
    wanted = [k.strip() for k in
              (cfg_get(cfg, "panel_gauges") or "").split(",") if k.strip()]
    by_key = {g["key"]: g for g in gauges}
    shown = [by_key[k] for k in wanted if k in by_key]
    return shown or gauges[:3]


def extract_gauges(data):
    """Normalize the API response into ordered gauge dicts."""
    if not isinstance(data, dict):
        return []

    limits = data.get("limits")
    if isinstance(limits, list):
        gauges = _gauges_from_limits(limits)
        if gauges:
            return gauges

    # Fallback: the flat bucket map, for a response with no usable `limits`.
    gauges = []
    seen = set()

    def add(key, label):
        bucket = data.get(key)
        if not isinstance(bucket, dict):
            return
        util = bucket.get("utilization")
        if not isinstance(util, (int, float)):
            return
        gauges.append({
            "key": key,
            "label": label,
            "utilization": float(util),
            "resets_at": bucket.get("resets_at"),
            "window": window_seconds(key),
            "group": None,
            "severity": None,
            "is_active": None,
        })
        seen.add(key)

    for key, label in BUCKET_LABELS:
        add(key, label)
    for key, val in data.items():
        if key in seen or key == "extra_usage" or not isinstance(val, dict):
            continue
        util = val.get("utilization")
        if not isinstance(util, (int, float)):
            continue
        # Unlaunched codename buckets ship as 0% with no window. Anything real
        # has either some usage or a reset time.
        if not util and not val.get("resets_at"):
            continue
        add(key, key.replace("_", " ").title())
    return gauges


def _parse_iso(value):
    """Parse an ISO-8601 timestamp (with 'Z' or an offset) to aware datetime."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _fmt_duration(secs):
    """45m / 3h 10m / 4d 6h — coarse, single-glance durations."""
    if secs < 90 * 60:
        return "{}m".format(max(1, int(secs // 60)))
    if secs < 48 * 3600:
        return "{}h {}m".format(int(secs // 3600), int((secs % 3600) // 60))
    return "{}d {}h".format(int(secs // 86400), int((secs % 86400) // 3600))


def fmt_reset_delta(resets_at):
    """'2026-06-11T19:30:00+00:00' -> '45m' / '3h 10m' / '4d 6h', or None."""
    dt = _parse_iso(resets_at) if resets_at else None
    if dt is None:
        return None
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    return "now" if secs <= 0 else _fmt_duration(secs)


def fmt_age(timestamp):
    """Same, for a timestamp in the past: '12m' / '3h 4m', or None."""
    dt = _parse_iso(timestamp) if timestamp else None
    if dt is None:
        return None
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    return "just now" if secs < 60 else _fmt_duration(secs)


def fmt_when(value):
    """Local wall-clock time for an epoch or ISO-8601 timestamp, e.g.
    'Sat 2026-10-25 14:30' — the tooltip behind a relative duration."""
    if isinstance(value, str):
        dt = _parse_iso(value)
        if dt is None:
            return None
        value = dt.timestamp()
    return datetime.fromtimestamp(value).strftime("%a %Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Platform status (status.claude.com, cached)
# ---------------------------------------------------------------------------

# Statuspage's overall indicator. Colour for the panel frame and the
# detail-window banner; "none" (and an unknown/absent value) paints nothing.
# A future indicator we don't know about gets the cautious amber rather than
# red — better to under-alarm than to invent an outage.
STATUS_COLORS = {
    "maintenance": (0.20, 0.47, 0.80),   # blue — informational
    "minor": (0.91, 0.55, 0.05),         # orange — degraded
    "major": (0.80, 0.16, 0.16),         # red — outage
    "critical": (0.60, 0.07, 0.07),      # deep red
}
STATUS_UNKNOWN_COLOR = STATUS_COLORS["minor"]


def platform_status_color(indicator):
    """(r,g,b) for an abnormal indicator, or None when nothing should show."""
    if not indicator or indicator == "none":
        return None
    return STATUS_COLORS.get(indicator, STATUS_UNKNOWN_COLOR)


def status_is_normal(status):
    return ((status or {}).get("indicator") or "") in ("", "none")


def status_headline(status):
    """'Partially Degraded Service', or None when all is well.

    Statuspage always sends a human description; fall back to a prettified
    indicator so a missing description can never hide a real outage.
    """
    if status_is_normal(status):
        return None
    return ((status.get("description") or "").strip()
            or (status.get("indicator") or "").replace("_", " ").title()
            or "Service disruption")


def fetch_platform_status(cfg, force=False):
    """Return {indicator, description, updated_at, fetched_at, stale, error}.

    Same cache-gates-the-network shape as fetch_official_usage, minus the 429
    machinery: this is a public CDN-served file, so a plain freshness gate plus
    a short retry delay on failure is enough. Unlike the usage call, force=True
    also skips that retry delay — there is no rate limit to respect here, and a
    user pressing "Refresh now" during an outage wants the current status, not
    the one from before the last network blip.

    A status page we cannot reach is never reported as a problem with *Claude*:
    the indicator falls back to the last one we saw, or to "" (treated as
    normal) if we have never seen one. The widget must not raise a false alarm
    because its own fetch failed.
    """
    refresh = max(60, cfg_int(cfg, "status_refresh_seconds", 300))
    now = time.time()

    cached = None
    try:
        with open(STATUS_CACHE) as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        pass

    def result(stale, error):
        c = cached or {}
        return {
            "indicator": c.get("indicator") or "",
            "description": c.get("description") or "",
            "updated_at": c.get("updated_at"),
            "fetched_at": c.get("fetched_at"),
            "stale": stale,
            "error": error,
        }

    if cached and not force:
        if now - (cached.get("fetched_at") or 0) < refresh:
            return result(False, None)
        if now < (cached.get("next_earliest") or 0):
            return result(True, cached.get("error"))

    req = urllib.request.Request(STATUS_URL, headers={"User-Agent": APP_ID})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
        st = payload.get("status") if isinstance(payload, dict) else None
        st = st if isinstance(st, dict) else {}
        page = payload.get("page") if isinstance(payload, dict) else None
        cached = {
            "fetched_at": now,
            "indicator": st.get("indicator") or "none",
            "description": (st.get("description") or "").strip(),
            "updated_at": (page or {}).get("updated_at"),
            "error": None,
            "next_earliest": 0,
        }
        _atomic_write_json(STATUS_CACHE, cached)
        return result(False, None)
    except urllib.error.HTTPError as exc:
        err = "status page returned HTTP {}".format(exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        err = "status page unreachable: {}".format(getattr(exc, "reason", exc))

    entry = dict(cached or {})
    entry.update({"error": err, "last_attempt": now,
                  "next_earliest": now + min(refresh, 120)})
    _atomic_write_json(STATUS_CACHE, entry)
    return result(True, err)


def open_status_page(parent=None):
    """Open the public status page in the user's default browser."""
    try:
        from gi.repository import Gtk, Gdk
        if Gtk.show_uri_on_window(parent, STATUS_PAGE_URL, Gdk.CURRENT_TIME):
            return
    except Exception:
        pass  # no portal / no handler registered — try xdg-open instead
    try:
        subprocess.Popen(["xdg-open", STATUS_PAGE_URL],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Local history database / incremental indexer (for the detail view)
# ---------------------------------------------------------------------------

def db_connect():
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files(
            path  TEXT PRIMARY KEY,
            mtime REAL,
            size  INTEGER,
            offset INTEGER
        );
        CREATE TABLE IF NOT EXISTS events(
            key          TEXT PRIMARY KEY,
            ts           INTEGER,
            model        TEXT,
            input        INTEGER,
            output       INTEGER,
            cache_create INTEGER,
            cache_read   INTEGER,
            cost         REAL
        );
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
        """
    )
    return conn


def _parse_ts(value):
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return int(datetime.fromisoformat(value).timestamp())
    except (ValueError, AttributeError):
        return None


def _parse_record(rec):
    """Return an event tuple or None if this line is not real usage."""
    msg = rec.get("message") or {}
    if msg.get("role") != "assistant":
        return None
    usage = msg.get("usage")
    if not usage:
        return None
    model = msg.get("model") or "?"
    if model == "<synthetic>":
        return None

    ts = _parse_ts(rec.get("timestamp"))
    if ts is None:
        return None

    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cache_create = int(usage.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)

    key = "{}|{}".format(rec.get("requestId") or "",
                         msg.get("id") or rec.get("uuid") or "")
    return (key, ts, model, inp, out, cache_create, cache_read, 0.0)


def index_new_data(conn, claude_dir):
    """Incrementally read appended JSONL bytes only. Returns (#new, #files)."""
    projects = os.path.join(os.path.expanduser(claude_dir), "projects")
    files = glob.glob(os.path.join(projects, "**", "*.jsonl"), recursive=True)

    known = {row[0]: row for row in conn.execute("SELECT path,mtime,size,offset FROM files")}
    new_count = 0
    rows = []

    for fp in files:
        try:
            st = os.stat(fp)
        except OSError:
            continue
        prev = known.get(fp)
        start = 0
        if prev:
            _, p_mtime, p_size, p_off = prev
            if st.st_size == p_size and st.st_mtime == p_mtime:
                continue                      # unchanged
            if st.st_size >= p_size:
                start = p_off                 # appended: read only the new bytes
            # else: file shrank/rotated -> reindex from 0

        try:
            with open(fp, "rb") as fh:
                fh.seek(start)
                data = fh.read()
        except OSError:
            continue

        if data.endswith(b"\n"):
            complete, consumed = data.split(b"\n")[:-1], len(data)
        else:
            pieces = data.split(b"\n")
            complete = pieces[:-1]
            consumed = len(data) - len(pieces[-1])

        for raw in complete:
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8", "ignore"))
            except (ValueError, UnicodeError):
                continue
            ev = _parse_record(rec)
            if ev:
                rows.append(ev)

        conn.execute(
            "INSERT INTO files(path,mtime,size,offset) VALUES(?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=?,size=?,offset=?",
            (fp, st.st_mtime, st.st_size, start + consumed,
             st.st_mtime, st.st_size, start + consumed),
        )

    if rows:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO events"
            "(key,ts,model,input,output,cache_create,cache_read,cost) "
            "VALUES(?,?,?,?,?,?,?,?)",
            rows,
        )
        new_count = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(rows)
    conn.commit()
    return new_count, len(files)


TOKENS_EXPR = "COALESCE(SUM(input+output+cache_create+cache_read),0)"


def series(conn, since, bucket_seconds):
    """Return [(bucket_start_ts, tokens), ...] across [since, now]."""
    return conn.execute(
        "SELECT (ts/?)*? AS b, {} v FROM events WHERE ts>=? "
        "GROUP BY b ORDER BY b".format(TOKENS_EXPR),
        (bucket_seconds, bucket_seconds, int(since)),
    ).fetchall()


def recent_events(conn, limit=500):
    return conn.execute(
        "SELECT ts,model,input,output,cache_create,cache_read "
        "FROM events ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()


def totals(conn):
    row = conn.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts), {} FROM events".format(TOKENS_EXPR)
    ).fetchone()
    return {"count": row[0] or 0, "first": row[1], "last": row[2],
            "tokens": row[3] or 0}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_tokens(n):
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return "{:.1f}{}".format(n / div, unit)
    return "{:.0f}".format(n)


def fmt_pct(util):
    return "{:.0f}%".format(util)


def short_model(model):
    """'claude-haiku-4-5-20251001' -> 'haiku-4-5'."""
    m = (model or "?").replace("claude-", "")
    parts = m.split("-")
    if parts and parts[-1].isdigit() and len(parts[-1]) >= 6:
        parts = parts[:-1]
    return "-".join(parts) or "?"


def fmt_datetime(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Bar colour: burn rate measured against the window clock
# ---------------------------------------------------------------------------

# Coolest to hottest. The blue is the old flat bar colour (it matches the
# VS Code popup), so "perfectly on pace" still looks like the familiar widget.
BAR_COLORS = {
    "under":    (0.24, 0.70, 0.35),   # green  — comfortably below pace
    "on_pace":  (0.26, 0.55, 0.95),   # blue   — tracking to land at the limit
    "ahead":    (0.93, 0.72, 0.13),   # yellow — will overshoot a little
    "hot":      (0.92, 0.45, 0.10),   # orange — will overshoot a lot
    "critical": (0.85, 0.20, 0.20),   # red    — will run dry well before reset
}
BAR_STALE_COLOR = (0.55, 0.55, 0.58)
SEVERITY_ORDER = ["under", "on_pace", "ahead", "hot", "critical"]

# How long each quota window runs. Needed to turn `resets_at` (the end of the
# window) into "how far through it are we". Unknown buckets get absolute-only
# colouring rather than a guessed window.
WINDOW_SECONDS = {"five_hour": 5 * 3600}

# Treat the window as opening with this much clock and this much quota already
# spent. Without the smoothing the raw ratio is nonsense in the opening
# minutes — 2% used three minutes into a five-hour window extrapolates to
# 200% — and the bar would flash red on the first prompt of every session.
PACE_GRACE = 0.08
# Below this share of the quota nothing is worth alarming about, whatever the
# arithmetic says.
PACE_MIN_USAGE = 0.05


def window_seconds(key):
    """Length of a quota window in seconds inferred from a flat bucket key, or
    None if we don't know it. Gauges built from the `limits` array carry their
    own window (taken from the entry's `group`) and don't need this."""
    if key in WINDOW_SECONDS:
        return WINDOW_SECONDS[key]
    if (key or "").startswith("seven_day"):
        return 7 * 86400
    return None


def window_elapsed(gauge, now=None):
    """Fraction (0..1] of the gauge's window that has gone, or None.

    None when the window length or its reset time is unknown, or when the reset
    time doesn't line up with the clock (further out than the window is long).
    This is also the utilization you would be at if you were exactly on pace.
    """
    length = gauge.get("window") or window_seconds(gauge.get("key"))
    if not length:
        return None
    dt = _parse_iso(gauge.get("resets_at"))
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    remaining = (dt - now).total_seconds()
    elapsed = min(1.0, (length - remaining) / float(length))
    return elapsed if elapsed > 0.0 else None


def pace_projection(gauge, now=None):
    """Smoothed end-of-window utilization, as a fraction of the limit.

    1.0 means "keep burning at this rate and you land exactly at 100% when the
    window resets"; 1.5 means you'd want half again as much quota as you have.
    Returns None when the window can't be placed on the clock (see
    window_elapsed).
    """
    elapsed = window_elapsed(gauge, now)
    if elapsed is None:
        return None
    used = max(0.0, gauge["utilization"] / 100.0)
    return (used + PACE_GRACE) / (elapsed + PACE_GRACE)


# Tick spacing for the detail-window bars: the smallest of these units that
# leaves a readable handful of divisions. A 5-hour window ticks hourly, a
# 7-day one daily; anything longer falls through to weekly.
TICK_UNITS = (3600, 86400, 7 * 86400)
TICK_MAX_DIVISIONS = 8


def window_ticks(gauge):
    """Interior tick positions (0..1 along the window) for a gauge's bar, at
    whole hours or days from the start of its window. Empty when the window
    length is unknown or would only produce a single division."""
    length = gauge.get("window") or window_seconds(gauge.get("key"))
    if not length:
        return []
    for unit in TICK_UNITS:
        divisions = length / float(unit)
        if divisions <= TICK_MAX_DIVISIONS:
            count = int(round(divisions))
            return [i * unit / float(length) for i in range(1, count)]
    return []


# Projections below this read as "on pace" (blue); at or above it, "over pace".
ON_PACE_LIMIT = 1.05


def pace_severity(projection):
    """Index into SEVERITY_ORDER for a projected end-of-window utilization."""
    if projection < 0.85:
        return 0   # green  — will finish with room to spare
    if projection < ON_PACE_LIMIT:
        return 1   # blue   — will finish about level with the limit
    if projection < 1.35:
        return 2   # yellow — over by up to a third
    if projection < 1.90:
        return 3   # orange — over by up to nearly double
    return 4       # red    — burning at twice the sustainable rate or more


def absolute_severity(used):
    """Severity from raw usage alone: the "Usage percentage only" colouring,
    and the fallback for a gauge whose pace can't be computed."""
    if used >= 0.95:
        return 4
    if used >= 0.85:
        return 3
    if used >= 0.75:
        return 2
    return 0


# Wording for the percentage-only fallback, used when there is no clock to
# judge pace by (no reset time, unknown window). Kept separate from the pace
# phrasing so the text can never contradict the colour.
ABSOLUTE_NOTES = {2: "running low", 3: "very low", 4: "nearly exhausted"}

# At or above this the quota is spent, which is a fact rather than a forecast:
# the bar is red whatever the clock says, because a projection that reads
# "on pace" at 100% used would be describing a limit you are already locked at.
EXHAUSTED_AT = 0.995


def pace_phrase(projection):
    """'under pace' / 'on pace' / 'over pace (~150% by reset)'."""
    if projection < 0.85:
        return "under pace"
    if projection < ON_PACE_LIMIT:
        return "on pace"
    return "over pace (~{:.0f}% by reset)".format(min(projection, 9.99) * 100)


def catch_up_seconds(gauge, mode="pace", now=None):
    """Seconds until an over-pace gauge reads "on pace" again, if no more of
    the quota is used in the meantime; None unless the gauge is over pace.

    With usage frozen the projection falls as the window clock runs, crossing
    ON_PACE_LIMIT where (used + grace) / (elapsed + grace) == ON_PACE_LIMIT.
    That is the moment the bar turns blue, so the countdown and the colour
    agree. It always lands before the reset, because an exhausted quota is
    never reported as over pace.
    """
    if mode == "absolute":
        return None
    used = max(0.0, gauge["utilization"] / 100.0)
    if used >= EXHAUSTED_AT or used < PACE_MIN_USAGE:
        return None
    elapsed = window_elapsed(gauge, now)
    if elapsed is None or pace_projection(gauge, now) < ON_PACE_LIMIT:
        return None
    length = gauge.get("window") or window_seconds(gauge.get("key"))
    target = (used + PACE_GRACE) / ON_PACE_LIMIT - PACE_GRACE
    # Round up to the minute: the exact crossing sits on the threshold, where
    # float error can leave the bar a tick short of blue.
    return 60.0 * max(1, math.ceil((target - elapsed) * length / 60.0))


def gauge_status(gauge, mode="pace", now=None):
    """(severity, note) for one gauge — the single source for both the bar
    colour and the words under it, so the text can't disagree with the colour.

    In pace mode the burn rate decides, full stop: a bar that is behind the
    clock is not in trouble however full it looks, because it will reach the
    reset with quota to spare. The raw percentage only takes over where pace
    can't be computed at all, and when the quota is already spent.
    """
    used = max(0.0, min(1.0, gauge["utilization"] / 100.0))
    floor = absolute_severity(used)
    if mode == "absolute":
        return floor, None
    if used >= EXHAUSTED_AT:
        return 4, ABSOLUTE_NOTES[4]
    if used < PACE_MIN_USAGE:
        return 0, None   # too little used for a rate to mean anything
    projection = pace_projection(gauge, now)
    if projection is None:
        return floor, ABSOLUTE_NOTES.get(floor)   # no clock: percentage only
    return pace_severity(projection), pace_phrase(projection)


def bar_color(gauge, stale=False, mode="pace", now=None):
    """(r,g,b) for one gauge's bar."""
    if stale:
        return BAR_STALE_COLOR
    if mode == "absolute":
        return pct_color(max(0.0, min(1.0, gauge["utilization"] / 100.0)))
    return BAR_COLORS[SEVERITY_ORDER[gauge_status(gauge, mode, now)[0]]]


def pace_note(gauge, mode="pace", now=None):
    """Text for under the bar. Always agrees with bar_color().

    'over pace (~120% by reset), catch up in 1h 5m' when over pace."""
    note = gauge_status(gauge, mode, now)[1]
    catch_up = catch_up_seconds(gauge, mode, now)
    if note and catch_up is not None:
        note += ", catch up in {}".format(_fmt_duration(catch_up))
    return note


def pct_color(frac):
    """(r,g,b) for a 0..1 usage fraction, ignoring the window clock."""
    if frac >= 0.90:
        return (0.85, 0.20, 0.20)   # red
    if frac >= 0.70:
        return (0.90, 0.62, 0.10)   # amber
    return (0.26, 0.55, 0.95)       # blue (matches the VS Code popup)


# Panel bar orientation. A bar reads as a bar while it is several times longer
# than it is thick; below about 4.5:1 the panel's shape has turned a horizontal
# row into a blob. An upright column needs the opposite — clearly taller than
# it is wide. Standing the bars up takes both, so a panel that suits neither
# shape is left horizontal rather than swapped for something worse.
HORIZONTAL_MIN_ASPECT = 4.5
VERTICAL_MIN_ASPECT = 1.6
# Side margin the percentages get when the image width is on 'auto'.
TEXT_MARGIN = 4
# Border painted in the platform-status colour when Claude is degraded. The
# bars tile the rest of the image, so there is no background left to tint.
PANEL_STATUS_FRAME = 3


def use_vertical_bars(layout, band_h, band_w, n_bars):
    """Should the panel bars be drawn as upright columns?

    'auto' weighs the two shapes against the space the bars actually get, which
    depends on the panel height *and* on how wide the percentages made the
    image — a big genmon font keeps horizontal rows long enough to work on a
    panel where a small one would have squashed them. 'horizontal' and
    'vertical' pin the choice.
    """
    choice = (layout or "auto").strip().lower()
    if choice in ("horizontal", "vertical"):
        return choice == "vertical"
    if n_bars < 1 or band_h <= 0 or band_w <= 0:
        return False
    bar_h = band_h / float(n_bars)   # thickness of one horizontal row
    col_w = band_w / float(n_bars)   # width of one upright column
    if band_w / bar_h >= HORIZONTAL_MIN_ASPECT:
        return False  # the rows are still comfortably bars; leave them alone
    return band_h / col_w >= VERTICAL_MIN_ASPECT


def rounded_rect(ctx, x, y, w, h, r):
    """Append a rounded rectangle to the current path (no-op if degenerate)."""
    if w <= 0 or h <= 0:
        return
    r = min(r, h / 2.0, w / 2.0)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def luminance(rgb):
    """Relative luminance of an (r,g,b) 0..1 tuple."""
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def contrast_text_colors(bg_rgb):
    """(text_rgb, halo_rgba) that stay legible on a solid bg_rgb fill."""
    if luminance(bg_rgb) < 0.5:
        return (0.98, 0.98, 0.98), (0.0, 0.0, 0.0, 0.55)
    return (0.08, 0.08, 0.08), (1.0, 1.0, 1.0, 0.55)


def rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(
        *(int(round(max(0.0, min(1.0, c)) * 255)) for c in rgb[:3]))


def resolve_text_colors(cfg):
    """Return (text_rgb, halo_rgba) for the panel numbers.

    Default is dark text with a soft light halo: high-contrast on the common
    light-grey panel, still legible on a dark panel thanks to the halo.
    """
    val = (cfg_get(cfg, "panel_text_color") or "auto").strip().lower()
    light = (0.97, 0.97, 0.97)
    dark = (0.10, 0.10, 0.10)
    if val.startswith("#") and len(val) in (4, 7):
        try:
            if len(val) == 4:
                rgb = tuple(int(val[i] * 2, 16) / 255.0 for i in (1, 2, 3))
            else:
                rgb = tuple(int(val[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
            halo = ((1, 1, 1, 0.75) if luminance(rgb) < 0.5
                    else (0, 0, 0, 0.65))
            return rgb, halo
        except ValueError:
            pass
    if val == "light":
        return light, (0.0, 0.0, 0.0, 0.65)
    # "auto" and "dark" -> dark text, light halo
    return dark, (1.0, 1.0, 1.0, 0.75)


# ---------------------------------------------------------------------------
# Panel image rendering (genmon mode) — pycairo only, no display needed
# ---------------------------------------------------------------------------

def _panel_xml_path():
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "xfce4", "xfconf", "xfce-perchannel-xml",
                        "xfce4-panel.xml")


# An xfce4-panel's `size` is the height of ONE row; a panel with `nrows` rows
# is that many times thicker. A plugin either keeps to a single row or spans
# them all, which for genmon is its `enable-single-row` property. Absent nrows
# means one row; absent enable-single-row is treated as single, because an
# image that is smaller than its row merely looks small, while one that is
# taller gets clipped or pushes the panel around.
PANEL_MAX_HEIGHT = 256
PANEL_MIN_HEIGHT = 16


def discover_genmon_context():
    """Find OUR genmon instance in the panel config; return (font, height, how).

    Matches the panel item whose command runs this script, then reads the font
    the user chose in genmon's Properties and works out how many pixels of
    panel thickness the item actually gets. Font and height may be None; `how`
    is a dict of what was found, for --self-test to print. Never raises.
    """
    how = {"xml": _panel_xml_path(), "plugin": None, "size": None,
           "nrows": None, "single_row": None, "spans": None}
    try:
        root = ET.parse(_panel_xml_path()).getroot()
    except (OSError, ET.ParseError) as exc:
        how["error"] = str(exc)
        return None, None, how

    def child(parent, name):
        for c in parent.findall("property"):
            if c.get("name") == name:
                return c
        return None

    plugins = child(root, "plugins")
    panels = child(root, "panels")
    if plugins is None:
        how["error"] = "no <plugins> section"
        return None, None, how

    needle = os.path.basename(SCRIPT_PATH)
    plugin_id = None
    font = None
    single_row = True
    for pl in plugins.findall("property"):
        if pl.get("value") != "genmon":
            continue
        cmd = child(pl, "command")
        if cmd is not None and needle in (cmd.get("value") or ""):
            name = pl.get("name") or ""
            how["plugin"] = name or None
            plugin_id = name.rsplit("-", 1)[-1] if "-" in name else None
            f = child(pl, "font")
            font = (f.get("value") if f is not None else None) or None
            sr = child(pl, "enable-single-row")
            if sr is not None:
                single_row = (sr.get("value") or "").lower() != "false"
            how["single_row"] = single_row
            break
    if plugin_id is None:
        how["error"] = "no genmon item runs {}".format(needle)
        return font, None, how

    height = None
    if panels is not None:
        for pa in panels.findall("property"):
            ids = child(pa, "plugin-ids")
            if ids is None:
                continue
            if plugin_id not in [v.get("value") for v in ids.findall("value")]:
                continue
            size = child(pa, "size")
            nrows = child(pa, "nrows")
            try:
                px = int(size.get("value")) if size is not None else None
            except (TypeError, ValueError):
                px = None
            try:
                rows = int(nrows.get("value")) if nrows is not None else 1
            except (TypeError, ValueError):
                rows = 1
            rows = max(1, rows)
            how["size"], how["nrows"] = px, rows
            if px:
                # One row for a single-row item, the whole stack otherwise.
                spans = 1 if single_row else rows
                how["spans"] = spans
                height = max(PANEL_MIN_HEIGHT,
                             min(PANEL_MAX_HEIGHT, px * spans))
            break
    if height is None and "error" not in how:
        how["error"] = "item not listed on any panel"
    return font, height, how


def resolve_panel_appearance(cfg):
    """Return (font_string, height_px), honoring config overrides first, then
    the genmon/panel settings, then sensible defaults."""
    need_font = (cfg_get(cfg, "panel_font") or "auto").lower() == "auto"
    need_size = (cfg_get(cfg, "panel_size") or "auto").lower() == "auto"
    disc_font, disc_height, _how = (discover_genmon_context()
                                    if (need_font or need_size)
                                    else (None, None, {}))

    font = (disc_font or "Sans 11") if need_font else cfg_get(cfg, "panel_font")
    height = (disc_height or 28) if need_size else cfg_int(cfg, "panel_size", 28)
    return font, max(PANEL_MIN_HEIGHT, min(PANEL_MAX_HEIGHT, height))


def _parse_font_fallback(font_str):
    """Best-effort (family, size_px, slant, weight) for cairo's toy font API,
    used only if Pango is unavailable."""
    import cairo
    tokens = (font_str or "Sans 11").split()
    size_pt = 11.0
    if tokens and tokens[-1].replace(".", "", 1).isdigit():
        size_pt = float(tokens.pop())
    styles = {"bold", "italic", "oblique", "light", "regular", "medium",
              "book", "semibold", "thin", "black", "condensed", "expanded"}
    slant = (cairo.FONT_SLANT_ITALIC
             if any(t.lower() in ("italic", "oblique") for t in tokens)
             else cairo.FONT_SLANT_NORMAL)
    weight = (cairo.FONT_WEIGHT_BOLD if any(t.lower() == "bold" for t in tokens)
              else cairo.FONT_WEIGHT_NORMAL)
    family = " ".join(t for t in tokens if t.lower() not in styles) or "Sans"
    return family, size_pt * 96.0 / 72.0, slant, weight


def render_panel_image(cfg, gauges, stale=False, status=None):
    """Draw the usage bars edge to edge with the percentages over the top.

    The bars tile the whole image — no padding, no separate caption band, no
    rounded corners — stacked as full-width rows on a normal panel and standing
    as full-height columns on a tall one (see use_vertical_bars). Bar edges land
    on whole pixels so neighbours meet exactly instead of leaving a seam.

    When the Claude platform is degraded the image gains a status-coloured
    frame rather than a status-coloured background, because the bars cover
    the background; a frame also survives a bar sitting at 100%.
    """
    import cairo

    shown = panel_gauges(cfg, gauges)
    if not shown:
        raise ValueError("no gauges")
    n_bars = len(shown)

    status_rgb = platform_status_color((status or {}).get("indicator"))
    text_rgb, halo_rgba = resolve_text_colors(cfg)
    track_rgba = (0, 0, 0, 0.30) if status_rgb is not None else (1, 1, 1, 0.16)

    label = " · ".join(fmt_pct(g["utilization"]) for g in shown)
    if stale:
        label += " !"

    font_str, height = resolve_panel_appearance(cfg)
    frame = PANEL_STATUS_FRAME if status_rgb is not None else 0
    max_text_h = max(8, height - 2 * frame)

    # With the width pinned, the label has to fit it — three percentages are a
    # lot wider than two, and clipped digits are worse than small ones. On
    # 'auto' the width follows the label instead, so nothing needs shrinking.
    width_cfg = (cfg_get(cfg, "panel_width") or "auto").strip().lower()
    fixed_width = (None if width_cfg == "auto"
                   else max(24, cfg_int(cfg, "panel_width", 110)))
    max_text_w = (None if fixed_width is None
                  else max(8, fixed_width - 2 * (TEXT_MARGIN + frame)))

    def fit_scale(w, h):
        """How much to shrink the font so the label fits the space, or 1.0."""
        scale = 1.0
        if h > max_text_h and h > 0:
            scale = min(scale, max_text_h / float(h))
        if max_text_w is not None and w > max_text_w and w > 0:
            scale = min(scale, max_text_w / float(w))
        return scale

    # Prefer Pango (real font family + size); fall back to cairo's toy font API.
    try:
        import gi
        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo
    except (ImportError, ValueError):
        Pango = PangoCairo = None

    # Grayscale (not subpixel) anti-aliasing: subpixel AA produces colored
    # red/green fringes on low-contrast text against a light-grey panel.
    font_opts = cairo.FontOptions()
    font_opts.set_antialias(cairo.ANTIALIAS_GRAY)

    measure_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8)
    mctx = cairo.Context(measure_surface)
    mctx.set_font_options(font_opts)

    layout = None
    if Pango is not None:
        fd = Pango.FontDescription.from_string(font_str)
        layout = PangoCairo.create_layout(mctx)
        PangoCairo.context_set_font_options(layout.get_context(), font_opts)
        layout.set_font_description(fd)
        layout.set_text(label, -1)
        text_w, text_h = layout.get_pixel_size()
        scale = fit_scale(text_w, text_h)
        if scale < 1.0:
            fd.set_size(max(1, int(fd.get_size() * scale)))
            layout.set_font_description(fd)
            text_w, text_h = layout.get_pixel_size()
    else:
        family, size_px, slant, weight = _parse_font_fallback(font_str)
        mctx.select_font_face(family, slant, weight)
        mctx.set_font_size(size_px)
        text_w = mctx.text_extents(label).width
        ascent, descent = mctx.font_extents()[0], mctx.font_extents()[1]
        text_h = ascent + descent
        scale = fit_scale(text_w, text_h)
        if scale < 1.0:
            size_px = max(1.0, size_px * scale)
            mctx.set_font_size(size_px)
            text_w = mctx.text_extents(label).width
            ascent, descent = mctx.font_extents()[0], mctx.font_extents()[1]
            text_h = ascent + descent
        fallback_size_px = size_px

    if fixed_width is not None:
        width = fixed_width
    else:
        width = int(math.ceil(max(48, text_w + 2 * (TEXT_MARGIN + frame))))

    text_h = int(math.ceil(text_h))

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    ctx.set_font_options(font_opts)

    if status_rgb is not None:
        ctx.rectangle(0, 0, width, height)
        ctx.set_source_rgb(*status_rgb)
        ctx.fill()

    band_x, band_y = float(frame), float(frame)
    band_w = max(1.0, width - 2.0 * frame)
    band_h = max(1.0, height - 2.0 * frame)
    vertical = use_vertical_bars(cfg_get(cfg, "panel_bar_layout"),
                                 band_h, band_w, n_bars)

    def slice_edges(total, i):
        """Whole-pixel [start, end) for bar i, so the bars tile without seams."""
        return (int(round(total * i / float(n_bars))),
                int(round(total * (i + 1) / float(n_bars))))

    color_mode = cfg_get(cfg, "bar_color_mode")
    for i, gauge in enumerate(shown):
        frac = max(0.0, min(1.0, gauge["utilization"] / 100.0))
        lo, hi = slice_edges(band_w if vertical else band_h, i)
        if vertical:
            track = (band_x + lo, band_y, hi - lo, band_h)
            filled = band_h * frac
            fill = (band_x + lo, band_y + band_h - filled, hi - lo, filled)
        else:
            track = (band_x, band_y + lo, band_w, hi - lo)
            fill = (band_x, band_y + lo, band_w * frac, hi - lo)
        ctx.rectangle(*track)
        ctx.set_source_rgba(*track_rgba)
        ctx.fill()
        if fill[2] > 0.3 and fill[3] > 0.3:
            ctx.rectangle(*fill)
            ctx.set_source_rgb(*bar_color(gauge, stale, color_mode))
            ctx.fill()

    # Percentages over the bars, centred across the image and sitting at the
    # top — in an upright layout that is the end the bars fill last, so the
    # numbers usually land on empty track rather than across a fill edge. The
    # halo keeps them legible wherever they do land.
    halo = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
    text_top = float(frame)
    tx = (width - text_w) / 2.0
    if layout is not None:
        PangoCairo.update_layout(ctx, layout)
        ctx.set_source_rgba(*halo_rgba)
        for dx, dy in halo:
            ctx.move_to(tx + dx, text_top + dy)
            PangoCairo.show_layout(ctx, layout)
        ctx.set_source_rgb(*text_rgb)
        ctx.move_to(tx, text_top)
        PangoCairo.show_layout(ctx, layout)
    else:
        family, _size_px, slant, weight = _parse_font_fallback(font_str)
        ctx.select_font_face(family, slant, weight)
        ctx.set_font_size(fallback_size_px)
        baseline = text_top + ctx.font_extents()[0]
        ctx.set_source_rgba(*halo_rgba)
        for dx, dy in halo:
            ctx.move_to(tx + dx, baseline + dy)
            ctx.show_text(label)
        ctx.set_source_rgb(*text_rgb)
        ctx.move_to(tx, baseline)
        ctx.show_text(label)

    surface.write_to_png(PANEL_PNG)


def _xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def build_tooltip(gauges, fetched_at, stale, error, status=None, mode="pace"):
    lines = []
    headline = status_headline(status)
    if headline:
        # Lead with the platform problem — it explains failures the quota
        # numbers can't. Nothing is added while everything is operational.
        lines.append("⚠ Claude platform: {}".format(headline))
        lines.append("")
    lines.append("Claude usage")
    for g in gauges:
        line = "{:<20} {:>4}".format(g["label"], fmt_pct(g["utilization"]))
        reset = fmt_reset_delta(g["resets_at"])
        if reset:
            line += "   resets in {}".format(reset)
        # A stale reading makes a stale verdict; the bars go grey for the same
        # reason, and the "stale data" line below says why.
        note = None if stale else pace_note(g, mode)
        if note:
            line += "   ({})".format(note)
        lines.append(line)
    if stale:
        lines.append("⚠ stale data{}".format(" — " + error if error else ""))
    if fetched_at:
        lines.append("Fetched {}  ·  click for details".format(
            datetime.fromtimestamp(fetched_at).strftime("%H:%M:%S")))
    return "\n".join(lines)


def run_genmon():
    cfg = load_config()
    detail_cmd = "python3 {} --detail".format(_sh_quote(SCRIPT_PATH))
    config_cmd = "python3 {} --config".format(_sh_quote(SCRIPT_PATH))

    try:
        data, fetched_at, stale, error = fetch_official_usage(cfg)
        gauges = extract_gauges(data)
    except Exception as exc:
        gauges, fetched_at, stale, error = [], None, True, str(exc)

    try:
        status = fetch_platform_status(cfg)
    except Exception:
        status = None  # platform status is decoration; never break the panel

    # Keep the local history index warm so the detail window opens instantly.
    try:
        conn = db_connect()
        index_new_data(conn, cfg_get(cfg, "claude_dir"))
        conn.close()
    except Exception:
        pass

    if not gauges:
        msg = "xfce-claude-quota: " + (error or "no usage data")
        headline = status_headline(status)
        if headline:
            msg = "⚠ Claude platform: {}\n\n{}".format(headline, msg)
        sys.stdout.write(
            "<txt>Claude n/a</txt>\n<tool>{}</tool>\n"
            "<click>{}</click>\n<txtclick>{}</txtclick>".format(
                _xml_escape(msg), config_cmd, config_cmd))
        return 0

    tooltip = _xml_escape(build_tooltip(gauges, fetched_at, stale, error, status,
                                        cfg_get(cfg, "bar_color_mode")))
    try:
        render_panel_image(cfg, gauges, stale, status)
        # <click> fires on the image/widget; <txtclick> covers any text fallback.
        sys.stdout.write(
            "<img>{}</img>\n<tool>{}</tool>\n"
            "<click>{}</click>\n<txtclick>{}</txtclick>".format(
                PANEL_PNG, tooltip, detail_cmd, detail_cmd))
    except Exception:
        session = gauges[0]
        sys.stdout.write(
            "<bar>{}</bar>\n<txt>{}</txt>\n<tool>{}</tool>\n"
            "<click>{}</click>\n<txtclick>{}</txtclick>".format(
                int(max(0, min(100, session["utilization"]))),
                _xml_escape(" · ".join(fmt_pct(g["utilization"]) for g in gauges[:2])),
                tooltip, detail_cmd, detail_cmd))
    return 0


# ---------------------------------------------------------------------------
# Self-test (no GUI)
# ---------------------------------------------------------------------------

def run_self_test():
    cfg = load_config()
    print("xfce-claude-quota self-test")
    print("  data dir      : {}".format(cfg_get(cfg, "claude_dir")))
    sub = read_subscription_type(cfg)
    if sub:
        print("  subscription  : {}".format(sub))

    data, _fetched_at, stale, error = fetch_official_usage(cfg, force=True)
    gauges = extract_gauges(data)
    print("  official usage{}:".format(" (STALE)" if stale else ""))
    if error:
        print("      error: {}".format(error))
    for g in gauges:
        reset = fmt_reset_delta(g["resets_at"])
        note = None if stale else pace_note(g, cfg_get(cfg, "bar_color_mode"))
        print("      {:<20} {:>5}{}{}".format(
            g["label"], fmt_pct(g["utilization"]),
            "   resets in {}".format(reset) if reset else "",
            "   {}".format(note) if note else ""))
    if not gauges:
        print("      (none)")

    font, height, how = discover_genmon_context()
    print("  panel item    : {}".format(how.get("plugin") or "NOT FOUND"))
    if how.get("error"):
        print("      {}".format(how["error"]))
        print("      looked in {}".format(how.get("xml")))
    else:
        print("      row size {} px x {} row(s), item spans {} -> {} px".format(
            how.get("size"), how.get("nrows"), how.get("spans"), height))
        print("      genmon font: {}".format(font or "(unset)"))
    eff_font, eff_height = resolve_panel_appearance(cfg)
    print("      rendering at {} px, font {!r}{}".format(
        eff_height, eff_font,
        "" if (cfg_get(cfg, "panel_size") or "auto").lower() == "auto"
        else "  (panel_size overridden in config)"))
    print("      bar direction: {}".format(cfg_get(cfg, "panel_bar_layout")))
    print("      panel bars: {}".format(
        ", ".join(g["key"] for g in panel_gauges(cfg, gauges)) or "(none)"))
    print("      available keys: {}".format(
        ", ".join(g["key"] for g in gauges) or "(none)"))

    status = fetch_platform_status(cfg, force=True)
    print("  platform      : {} [{}]{}".format(
        status["description"] or "unknown",
        status["indicator"] or "unknown",
        "  (stale — {})".format(status["error"]) if status["error"] else ""))

    conn = db_connect()
    t0 = time.time()
    new, nfiles = index_new_data(conn, cfg_get(cfg, "claude_dir"))
    dt = time.time() - t0
    tot = totals(conn)
    conn.close()
    print("  local history : {} jsonl files, {} new records ({:.2f}s), "
          "{} events total".format(nfiles, new, dt, tot["count"]))
    if tot["first"]:
        print("  history span  : {}  ->  {}".format(
            fmt_datetime(tot["first"]), fmt_datetime(tot["last"])))
    if nfiles == 0:
        print("\n  WARNING: no .jsonl files found — check the data dir "
              "(Preferences / --config).")
    return 0


# ---------------------------------------------------------------------------
# GTK: detailed window + preferences
# ---------------------------------------------------------------------------

def _require_gtk():
    import gi
    gi.require_version("Gtk", "3.0")


RANGES = [
    ("Last 6 hours", 6 * 3600, 10 * 60),
    ("Last 24 hours", 24 * 3600, 60 * 60),
    ("Last 7 days", 7 * 86400, 6 * 3600),
    ("Last 30 days", 30 * 86400, 86400),
]


def build_detail_window(app):
    from gi.repository import Gtk, Gdk, GLib

    conn = db_connect()

    class Chart(Gtk.DrawingArea):
        def __init__(self):
            super().__init__()
            self.points = []
            self.set_size_request(560, 200)
            self.connect("draw", self.on_draw)

        def set_data(self, points):
            self.points = points
            self.queue_draw()

        def on_draw(self, _w, ctx):
            alloc = self.get_allocation()
            W, H = alloc.width, alloc.height
            ml, mr, mt, mb = 64, 12, 12, 28
            pw, ph = W - ml - mr, H - mt - mb

            ctx.set_source_rgb(0.12, 0.12, 0.14)
            ctx.paint()

            if not self.points or pw <= 0 or ph <= 0:
                ctx.set_source_rgb(0.6, 0.6, 0.6)
                ctx.select_font_face("Sans", 0, 0)
                ctx.set_font_size(13)
                msg = "No usage in this range."
                ext = ctx.text_extents(msg)
                ctx.move_to((W - ext.width) / 2, H / 2)
                ctx.show_text(msg)
                return

            vmax = (max(v for _, v in self.points) or 1.0) * 1.10

            ctx.set_font_size(10)
            for i in range(5):
                gy = mt + ph * i / 4.0
                ctx.set_source_rgba(1, 1, 1, 0.08)
                ctx.set_line_width(1)
                ctx.move_to(ml, gy)
                ctx.line_to(ml + pw, gy)
                ctx.stroke()
                lbl = fmt_tokens(vmax * (1 - i / 4.0))
                ctx.set_source_rgba(1, 1, 1, 0.55)
                ext = ctx.text_extents(lbl)
                ctx.move_to(ml - ext.width - 6, gy + 4)
                ctx.show_text(lbl)

            n = len(self.points)

            def px(i):
                return ml + (pw * i / (n - 1) if n > 1 else pw / 2)

            def py(v):
                return mt + ph * (1 - v / vmax)

            ctx.move_to(px(0), mt + ph)
            for i, (_, v) in enumerate(self.points):
                ctx.line_to(px(i), py(v))
            ctx.line_to(px(n - 1), mt + ph)
            ctx.close_path()
            ctx.set_source_rgba(0.26, 0.55, 0.95, 0.22)
            ctx.fill()

            ctx.set_source_rgb(0.36, 0.66, 1.0)
            ctx.set_line_width(2)
            for i, (_, v) in enumerate(self.points):
                (ctx.move_to if i == 0 else ctx.line_to)(px(i), py(v))
            ctx.stroke()

            ctx.set_source_rgba(1, 1, 1, 0.55)
            ctx.set_font_size(10)
            for i in (0, n // 2, n - 1):
                ts = self.points[i][0]
                span = self.points[-1][0] - self.points[0][0]
                fmt = "%H:%M" if span <= 36 * 3600 else "%m-%d"
                lbl = datetime.fromtimestamp(ts).strftime(fmt)
                ext = ctx.text_extents(lbl)
                x = min(max(px(i) - ext.width / 2, ml), ml + pw - ext.width)
                ctx.move_to(x, mt + ph + 18)
                ctx.show_text(lbl)

    class Bar(Gtk.DrawingArea):
        """One usage bar, drawn the same way (and in the same colours) as the
        panel widget — a themed Gtk.LevelBar can't be recoloured reliably.

        The bar's horizontal axis is the quota window as well as the quota:
        `ticks` are hour/day boundaries along it, and `pace` (the fraction of
        the window already gone) is where the fill would end if usage were
        exactly on pace. Fill running past the triangle is over pace."""

        HEIGHT = 12
        MARKER_H = 7     # triangle below the bar
        MARKER_W = 9

        def __init__(self, frac, rgb, ticks=(), pace=None):
            super().__init__()
            self.frac = max(0.0, min(1.0, frac))
            self.rgb = rgb
            self.ticks = list(ticks)
            self.pace = None if pace is None else max(0.0, min(1.0, pace))
            self.set_size_request(
                -1, self.HEIGHT + (self.MARKER_H + 1 if self.pace is not None else 0))
            if self.pace is not None:
                self.set_tooltip_text(
                    "Window {:.0f}% elapsed — the triangle is where usage "
                    "would be if exactly on pace".format(self.pace * 100))
            self.connect("draw", self.on_draw)

        def on_draw(self, _w, ctx):
            alloc = self.get_allocation()
            w, h = alloc.width, float(self.HEIGHT)
            y = 0.0
            # Track tinted from the theme's text colour so it reads as a
            # groove under both light and dark GTK themes.
            fg = self.get_style_context().get_color(self.get_state_flags())
            rounded_rect(ctx, 0, y, w, h, h / 2)
            ctx.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
            ctx.fill()
            fill_w = w * self.frac
            if fill_w > 0.5:
                rounded_rect(ctx, 0, y, fill_w, h, h / 2)
                ctx.set_source_rgb(*self.rgb)
                ctx.fill()

            # Hour/day boundaries, clipped to the bar so they follow its
            # rounded ends. Translucent theme-foreground, so they show on the
            # empty track and the coloured fill alike without shouting.
            if self.ticks:
                ctx.save()
                rounded_rect(ctx, 0, y, w, h, h / 2)
                ctx.clip()
                ctx.set_source_rgba(fg.red, fg.green, fg.blue, 0.38)
                ctx.set_line_width(1)
                for t in self.ticks:
                    x = round(w * t) + 0.5   # half-pixel: crisp 1px line
                    ctx.move_to(x, y)
                    ctx.line_to(x, y + h)
                    ctx.stroke()
                ctx.restore()

            if self.pace is not None:
                x = w * self.pace
                half = self.MARKER_W / 2.0
                # Keep the whole triangle inside the widget at either end.
                x = max(half, min(w - half, x))
                tip = y + h + 0.5
                ctx.move_to(x, tip)
                ctx.line_to(x - half, tip + self.MARKER_H)
                ctx.line_to(x + half, tip + self.MARKER_H)
                ctx.close_path()
                ctx.set_source_rgba(fg.red, fg.green, fg.blue, 0.85)
                ctx.fill()

    win = Gtk.ApplicationWindow(application=app, title="Claude Usage")
    win.set_default_size(680, 680)
    win.set_icon_name("utilities-system-monitor")

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_border_width(12)
    win.add(box)

    # --- Platform status banner: hidden while Claude is healthy ---
    # no_show_all keeps win.show_all() from revealing it; the children are
    # shown once here, so later show()/hide() on the EventBox is the only
    # visibility switch. The background comes from a CSS provider scoped to
    # this widget alone (no screen-wide provider), the foreground from markup.
    banner = Gtk.EventBox()
    banner.set_name("cq-status-banner")
    banner.set_no_show_all(True)
    banner.set_tooltip_text("Open {} in your browser".format(STATUS_PAGE_URL))
    banner_css = Gtk.CssProvider()
    banner.get_style_context().add_provider(
        banner_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    banner_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
    banner_inner.set_border_width(9)
    banner_label = Gtk.Label(xalign=0)
    banner_label.set_use_markup(True)
    banner_label.set_line_wrap(True)
    banner_inner.pack_start(banner_label, False, False, 0)
    banner.add(banner_inner)
    banner_inner.show_all()
    box.pack_start(banner, False, False, 0)

    banner.connect("button-press-event",
                   lambda *_: (open_status_page(win), True)[1])
    banner.connect("realize", lambda w: w.get_window().set_cursor(
        Gdk.Cursor.new_from_name(w.get_display(), "pointer")))

    def apply_platform_status(status):
        """Show/refresh/hide the banner for a fetched status dict."""
        color = platform_status_color((status or {}).get("indicator"))
        headline = status_headline(status)
        if color is None or not headline:
            banner.hide()
            return
        fg, _halo = contrast_text_colors(color)
        try:
            banner_css.load_from_data(
                "#cq-status-banner {{ background-color: {}; border-radius: 4px; }}"
                .format(rgb_to_hex(color)).encode())
        except Exception:
            pass  # no colour is better than no banner
        note = "Claude platform status — click to open {}".format(STATUS_PAGE_URL)
        age = fmt_age(status.get("updated_at"))
        tip = "Open {} in your browser".format(STATUS_PAGE_URL)
        if age:
            note += "  ·  updated {}".format(
                age if age == "just now" else age + " ago")
            tip += "\nStatus updated {}".format(fmt_when(status["updated_at"]))
        banner.set_tooltip_text(tip)
        banner_label.set_markup(
            "<span foreground='{}'><b>⚠  {}</b>\n<small>{}</small></span>".format(
                rgb_to_hex(fg), GLib.markup_escape_text(headline),
                GLib.markup_escape_text(note)))
        banner.show()

    header = Gtk.Label(xalign=0)
    header.set_use_markup(True)
    box.pack_start(header, False, False, 0)

    # --- Official gauges (rebuilt on each refresh) ---
    gauges_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.pack_start(gauges_box, False, False, 0)

    status = Gtk.Label(xalign=0)
    status.set_use_markup(True)
    box.pack_start(status, False, False, 0)

    link_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    link = Gtk.LinkButton.new_with_label(MANAGE_URL, "Manage usage on claude.ai")
    link.set_halign(Gtk.Align.START)
    link_row.pack_start(link, False, False, 0)
    refresh_btn = Gtk.Button(label="Refresh now")
    prefs_btn = Gtk.Button(label="Preferences…")
    link_row.pack_end(prefs_btn, False, False, 0)
    link_row.pack_end(refresh_btn, False, False, 0)
    box.pack_start(link_row, False, False, 0)

    box.pack_start(Gtk.Separator(), False, False, 4)

    # --- Local history ---
    hist_hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    hist_lbl = Gtk.Label(xalign=0)
    hist_lbl.set_markup("<b>Recent activity</b> <small>(local Claude Code "
                        "transcripts, tokens)</small>")
    hist_hdr.pack_start(hist_lbl, False, False, 0)
    range_combo = Gtk.ComboBoxText()
    for name, _, _ in RANGES:
        range_combo.append_text(name)
    range_combo.set_active(1)
    hist_hdr.pack_end(range_combo, False, False, 0)
    box.pack_start(hist_hdr, False, False, 0)

    chart = Chart()
    box.pack_start(chart, True, True, 0)

    store = Gtk.ListStore(str, str, str, str, str, str)
    tree = Gtk.TreeView(model=store)
    for i, (title, w) in enumerate([
        ("Time", 150), ("Model", 110), ("Input", 80), ("Output", 80),
        ("Cache", 90), ("Total", 90),
    ]):
        rend = Gtk.CellRendererText()
        if i >= 2:
            rend.set_property("xalign", 1.0)
        col = Gtk.TreeViewColumn(title, rend, text=i)
        col.set_resizable(True)
        col.set_min_width(w)
        tree.append_column(col)
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroll.set_min_content_height(180)
    scroll.add(tree)
    box.pack_start(scroll, True, True, 0)

    state = {"refreshing": False}

    def rebuild_gauges(gauges):
        color_mode = cfg_get(load_config(), "bar_color_mode")
        for child in gauges_box.get_children():
            gauges_box.remove(child)
        if not gauges:
            lbl = Gtk.Label(label="No usage data available.", xalign=0)
            gauges_box.pack_start(lbl, False, False, 0)
        for g in gauges:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            name = Gtk.Label(xalign=0)
            name.set_markup("<b>{}</b>".format(GLib.markup_escape_text(g["label"])))
            pct = Gtk.Label(xalign=1)
            pct.set_markup("<b>{}</b>".format(fmt_pct(g["utilization"])))
            top.pack_start(name, True, True, 0)
            top.pack_end(pct, False, False, 0)
            row.pack_start(top, False, False, 0)
            bar = Bar(g["utilization"] / 100.0, bar_color(g, mode=color_mode),
                      ticks=window_ticks(g), pace=window_elapsed(g))
            row.pack_start(bar, False, False, 0)
            # (text, tooltip) runs: each duration is its own label, so it can
            # carry a tooltip with the wall-clock time it stands for.
            pieces = []
            reset = fmt_reset_delta(g["resets_at"])
            if reset:
                pieces += [("Resets in ", None), (reset, fmt_when(g["resets_at"]))]
            note = gauge_status(g, color_mode)[1]
            if note:
                if pieces:
                    pieces.append(("  ·  ", None))
                pieces.append((note, None))
                catch_up = catch_up_seconds(g, color_mode)
                if catch_up is not None:
                    pieces += [(", catch up in ", None),
                               (_fmt_duration(catch_up),
                                "{} — if no more quota is used before then".format(
                                    fmt_when(time.time() + catch_up)))]
            if pieces:
                sub = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                for text, tip in pieces:
                    lbl = Gtk.Label(xalign=0)
                    lbl.set_markup("<small>{}</small>".format(
                        GLib.markup_escape_text(text)))
                    if tip:
                        lbl.set_tooltip_text(tip)
                    sub.pack_start(lbl, False, False, 0)
                row.pack_start(sub, False, False, 0)
            gauges_box.pack_start(row, False, False, 0)
        gauges_box.show_all()

    def refresh(force=False):
        if state["refreshing"]:
            return
        state["refreshing"] = True
        try:
            cfg2 = load_config()
            data, fetched_at, stale, error = fetch_official_usage(cfg2, force=force)
            rebuild_gauges(extract_gauges(data))

            try:
                apply_platform_status(fetch_platform_status(cfg2, force=force))
            except Exception:
                banner.hide()

            sub = read_subscription_type(cfg2)
            header.set_markup("<big><b>Claude usage</b></big>{}".format(
                "  <small>({} plan)</small>".format(GLib.markup_escape_text(sub))
                if sub else ""))

            bits = []
            if fetched_at:
                bits.append("fetched {}".format(
                    datetime.fromtimestamp(fetched_at).strftime("%H:%M:%S")))
            if stale:
                bits.append(
                    "<span foreground='#a40000' weight='bold'>stale{}</span>".format(
                        " — " + GLib.markup_escape_text(error) if error else ""))
            status.set_markup("<small>{}</small>".format(" · ".join(bits) or " "))

            try:
                index_new_data(conn, cfg_get(cfg2, "claude_dir"))
            except Exception:
                pass
            idx = range_combo.get_active()
            _, span, bucket = RANGES[idx if idx >= 0 else 1]
            since = int(time.time()) - span
            chart.set_data([(int(b), v) for b, v in series(conn, since, bucket)])

            store.clear()
            for ts, model, inp, out, cc, cr in recent_events(conn, 800):
                store.append([
                    fmt_datetime(ts), short_model(model),
                    fmt_tokens(inp), fmt_tokens(out),
                    fmt_tokens(cc + cr), fmt_tokens(inp + out + cc + cr),
                ])
        finally:
            state["refreshing"] = False

    refresh_btn.connect("clicked", lambda *_: refresh(force=True))
    prefs_btn.connect("clicked",
                      lambda *_: show_preferences(parent=win, on_save=refresh))
    range_combo.connect("changed", lambda *_: refresh())
    win.connect("destroy", lambda *_: conn.close())

    refresh()
    GLib.timeout_add_seconds(30, lambda: (refresh(), True)[1])

    win.show_all()
    if os.environ.get("XFCE_CQ_SMOKE"):
        GLib.timeout_add(1500, win.destroy)
    return win


def run_detail():
    _require_gtk()
    from gi.repository import Gtk

    # Single instance via Gtk.Application: clicking the panel again focuses
    # the existing window instead of opening another copy.
    app = Gtk.Application(application_id=DBUS_APP_ID)

    def on_activate(application):
        win = application.get_active_window()
        if win is None:
            win = build_detail_window(application)
        win.present()

    app.connect("activate", on_activate)
    return app.run([])


def show_preferences(parent=None, on_save=None):
    from gi.repository import Gtk

    cfg = load_config()
    dlg = Gtk.Dialog(title="Claude Quota — Preferences",
                     transient_for=parent, modal=True)
    dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                    Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
    dlg.set_default_size(460, -1)
    grid = Gtk.Grid(column_spacing=10, row_spacing=10, border_width=14)
    dlg.get_content_area().add(grid)

    row = 0

    def add_row(label, widget):
        nonlocal row
        lbl = Gtk.Label(label=label, xalign=0)
        grid.attach(lbl, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        widget.set_hexpand(True)
        row += 1
        return widget

    dir_entry = Gtk.Entry(text=cfg_get(cfg, "claude_dir"))
    add_row("Claude data directory", dir_entry)

    api_spin = Gtk.SpinButton.new_with_range(30, 1800, 15)
    api_spin.set_value(cfg_int(cfg, "api_refresh_seconds", 120))
    add_row("Usage API refresh (seconds)", api_spin)

    status_spin = Gtk.SpinButton.new_with_range(60, 3600, 30)
    status_spin.set_value(cfg_int(cfg, "status_refresh_seconds", 300))
    add_row("Platform status refresh (seconds)", status_spin)

    pfont_entry = Gtk.Entry(text=cfg_get(cfg, "panel_font"))
    pfont_entry.set_placeholder_text("auto = use the Generic Monitor font")
    add_row("Panel number font ('auto' or e.g. 'Sans Bold 14')", pfont_entry)

    pcolor_combo = Gtk.ComboBoxText.new_with_entry()
    for opt in ("auto", "dark", "light"):
        pcolor_combo.append_text(opt)
    pcolor_combo.get_child().set_text(cfg_get(cfg, "panel_text_color"))
    add_row("Panel number colour ('auto'/'dark'/'light'/#rrggbb)", pcolor_combo)

    psize_entry = Gtk.Entry(text=cfg_get(cfg, "panel_size"))
    add_row("Panel image height (px or 'auto')", psize_entry)

    pwidth_entry = Gtk.Entry(text=cfg_get(cfg, "panel_width"))
    add_row("Panel image width (px or 'auto')", pwidth_entry)

    gauges_entry = Gtk.Entry(text=cfg_get(cfg, "panel_gauges"))
    gauges_entry.set_placeholder_text("five_hour,seven_day,seven_day_fable")
    gauges_entry.set_tooltip_text(
        "Comma-separated bucket keys, in the order they are drawn.\n"
        "Run --self-test to see the keys the API is currently returning;\n"
        "keys that aren't in the response are skipped.")
    add_row("Panel bars (bucket keys)", gauges_entry)

    BAR_LAYOUTS = [("auto", "Auto (follow the panel's shape)"),
                   ("horizontal", "Always horizontal"),
                   ("vertical", "Always vertical")]
    blayout_combo = Gtk.ComboBoxText()
    for _key, text in BAR_LAYOUTS:
        blayout_combo.append_text(text)
    blayout_combo.set_active(next(
        (i for i, (k, _t) in enumerate(BAR_LAYOUTS)
         if k == (cfg_get(cfg, "panel_bar_layout") or "auto").strip().lower()), 0))
    add_row("Panel bar direction", blayout_combo)

    BAR_MODES = [("pace", "Burn rate vs. the window clock"),
                 ("absolute", "Usage percentage only")]
    bmode_combo = Gtk.ComboBoxText()
    for _key, text in BAR_MODES:
        bmode_combo.append_text(text)
    bmode_combo.set_active(
        0 if cfg_get(cfg, "bar_color_mode") != "absolute" else 1)
    add_row("Bar colours", bmode_combo)

    info = Gtk.Label(xalign=0)
    info.set_line_wrap(True)
    info.set_markup(
        "<small>No API key is needed: percentages come from Anthropic's usage "
        "endpoint using the login Claude Code already stores, history is "
        "read from local transcripts, and platform status from the public "
        "status.claude.com page. With <b>font</b> and <b>height</b> set to "
        "'auto', the widget follows the Generic Monitor item's own Font setting "
        "and the size of its panel (right-click the item → <i>Properties</i>). "
        "The bars tile the whole image and the percentages are drawn over them. "
        "<b>Panel bars</b> lists which buckets get one, in order. "
        "<b>Panel bar direction</b> on <i>auto</i> stacks them as rows on a "
        "normal panel and stands them up as columns once the panel is tall "
        "enough that rows would just be blobs. "
        "<b>Bar colours</b> on <i>burn rate</i> compare how much of the quota "
        "is gone against how much of the window has elapsed: green under pace, "
        "blue on pace, then yellow / orange / red as the projected end-of-window "
        "usage climbs past 100%. "
        "The <b>panel redraw rate</b> is genmon's <i>Period</i>; the API is "
        "contacted at most once per the interval above.</small>")
    grid.attach(info, 0, row, 2, 1)

    dlg.show_all()
    resp = dlg.run()
    if resp == Gtk.ResponseType.OK:
        cfg.set("general", "claude_dir",
                dir_entry.get_text().strip() or DEFAULTS["claude_dir"])
        cfg.set("general", "api_refresh_seconds", str(int(api_spin.get_value())))
        cfg.set("general", "status_refresh_seconds",
                str(int(status_spin.get_value())))
        cfg.set("general", "panel_font", pfont_entry.get_text().strip() or "auto")
        cfg.set("general", "panel_text_color",
                pcolor_combo.get_child().get_text().strip() or "auto")
        cfg.set("general", "panel_size", psize_entry.get_text().strip() or "auto")
        cfg.set("general", "panel_width", pwidth_entry.get_text().strip() or "auto")
        cfg.set("general", "bar_color_mode",
                BAR_MODES[max(0, bmode_combo.get_active())][0])
        cfg.set("general", "panel_bar_layout",
                BAR_LAYOUTS[max(0, blayout_combo.get_active())][0])
        cfg.set("general", "panel_gauges",
                gauges_entry.get_text().strip() or DEFAULTS["panel_gauges"])
        save_config(cfg)
        if on_save:
            on_save()
    dlg.destroy()


def run_config():
    _require_gtk()
    if not os.path.exists(CONFIG_FILE):
        save_config(load_config())
    show_preferences(parent=None, on_save=None)
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv):
    parser = argparse.ArgumentParser(
        description="Claude usage widget for the XFCE panel (via genmon).")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--detail", action="store_true", help="open the detailed window")
    g.add_argument("--config", action="store_true", help="open Preferences")
    g.add_argument("--self-test", action="store_true", help="print a summary")
    g.add_argument("--reindex", action="store_true",
                   help="rebuild the local-history cache and exit")
    g.add_argument("--print-genmon-command", action="store_true",
                   help="print the command to paste into genmon")
    args = parser.parse_args(argv)

    if args.print_genmon_command:
        print("python3 {}".format(_sh_quote(SCRIPT_PATH)))
        return 0
    if args.reindex:
        try:
            os.remove(DB_FILE)
        except OSError:
            pass
        return run_self_test()
    if args.self_test:
        return run_self_test()
    if args.config:
        return run_config()
    if args.detail:
        return run_detail()
    return run_genmon()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
