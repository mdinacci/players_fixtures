#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import hashlib
from datetime import datetime, timedelta, timezone
from dateutil import tz, parser as dtparser
import requests

LOCAL_TZ = tz.gettz("America/Los_Angeles")

# ----- Player -> (Club, League code, ESPN team ID)
PLAYERS = {
    "Pedri": ("Barcelona", "esp.1", 83),
    "Valverde": ("Real Madrid", "esp.1", 86),
    "Tonali": ("Newcastle United", "eng.1", 361),
    "Gravenberch": ("Liverpool", "eng.1", 364),
    "Caicedo": ("Chelsea", "eng.1", 363),
    "Rice": ("Arsenal", "eng.1", 359),
    "Rodri": ("Manchester City", "eng.1", 382),
    "Zubimendi": ("Arsenal", "eng.1", 359),  # updated
    "Lobotka": ("Napoli", "ita.1", 114),
    "Tchouaméni": ("Real Madrid", "esp.1", 86),
    "De Jong": ("Barcelona", "esp.1", 83),
    "Barella": ("Internazionale", "ita.1", 110),
    "Éderson (Atalanta)": ("Atalanta", "ita.1", 105),
}
CLUBS = {(club, league, tid) for (_, (club, league, tid)) in PLAYERS.items()}

def espn_scoreboard(league_code: str, start: datetime, end: datetime):
    def ymd(d): return d.strftime("%Y%m%d")
    url = (
        f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_code}/scoreboard"
        f"?dates={ymd(start)}-{ymd(end)}"
    )
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    data = r.json()
    return data.get("events", []) or []

def extract_team_events(events, team_id: int):
    """Return upcoming events for team_id as dicts with utc/local/home/away/competition."""
    now_utc = datetime.now(timezone.utc)
    found = []
    for ev in events:
        try:
            start_iso = ev.get("date")
            start_dt = dtparser.isoparse(start_iso) if start_iso else None
            comps = (ev.get("competitions") or [{}])[0]
            competitors = comps.get("competitors", [])
            ids = []
            sides = {}
            for c in competitors:
                t = c.get("team", {}) or {}
                if "id" in t:
                    ids.append(int(t["id"]))
                sides[c.get("homeAway")] = t.get("displayName")
            if team_id in ids and start_dt and start_dt > now_utc:
                comp_name = (comps.get("league") or {}).get("name") or ev.get("name") or ""
                # ESPN sometimes has venue info
                venue = (comps.get("venue") or {}).get("fullName") or ""
                found.append({
                    "utc": start_dt.astimezone(timezone.utc),
                    "home": sides.get("home"),
                    "away": sides.get("away"),
                    "competition": comp_name,
                    "venue": venue,
                })
        except Exception:
            continue
    found.sort(key=lambda x: x["utc"])
    return found

def gather_next_two_per_club():
    """Fetch all leagues once, return dict club -> [events (len<=2)]."""
    # Next 28 days
    start_local = datetime.now(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start = start_local.astimezone(timezone.utc)
    end = start + timedelta(days=28)

    leagues_cache = {}
    for (_, league, _) in CLUBS:
        if league not in leagues_cache:
            leagues_cache[league] = espn_scoreboard(league, start, end)

    result = {}
    for club, league, tid in sorted(CLUBS):
        events = extract_team_events(leagues_cache.get(league, []), tid)[:2]
        result[(club, league, tid)] = events
    return result

# ---------- ICS helpers

def ics_escape(text: str) -> str:
    """Escape text per RFC5545."""
    if text is None:
        return ""
    return (
        text.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;")
    )

def fold_lines(s: str) -> str:
    """Fold lines at 75 octets with CRLF continuation (simple approximation)."""
    out_lines = []
    for line in s.splitlines():
        b = line.encode("utf-8")
        if len(b) <= 75:
            out_lines.append(line)
        else:
            # naive fold by characters while keeping bytes <= 75
            current = ""
            for ch in line:
                if len((current + ch).encode("utf-8")) > 75:
                    out_lines.append(current)
                    current = " " + ch  # continuation line starts with one space
                else:
                    current += ch
            if current:
                out_lines.append(current)
    return "\r\n".join(out_lines)

def event_uid(club: str, e: dict) -> str:
    raw = f"{club}-{e['utc'].isoformat()}-{e['home']}vs{e['away']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() + "@fixtures"

def to_ics(events_by_club: dict, cal_name="Study Players — Next Two Fixtures") -> str:
    now = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Marco Fixtures//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(cal_name)}",
        "X-WR-TIMEZONE:UTC"
    ]
    for (club, league, _tid), evs in events_by_club.items():
        for e in evs:
            dtstart = e["utc"].strftime("%Y%m%dT%H%M%SZ")
            # assume 2 hours default duration if no explicit end
            dtend = (e["utc"] + timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ")
            summary = f"{e['home']} vs {e['away']} — {club}"
            desc = f"{e['competition']}"
            location = e.get("venue") or e['home']  # best effort
            uid = event_uid(club, e)
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{now}",
                f"DTSTART:{dtstart}",
                f"DTEND:{dtend}",
                f"SUMMARY:{ics_escape(summary)}",
                f"DESCRIPTION:{ics_escape(desc)}",
                f"LOCATION:{ics_escape(location)}",
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return fold_lines("\r\n".join(lines) + "\r\n")

# ---------- CLI and (optional) server

def build_ics() -> str:
    data = gather_next_two_per_club()
    return to_ics(data)

def main():
    ap = argparse.ArgumentParser(description="Generate or serve an ICS calendar of next two fixtures per club.")
    ap.add_argument("--out", default="fixtures.ics", help="Path to write .ics (default: fixtures.ics)")
    ap.add_argument("--serve", action="store_true", help="Serve ICS at http://localhost:8000/fixtures.ics")
    args = ap.parse_args()

    if args.serve:
        # lazy import to avoid dependency unless needed
        from flask import Flask, Response
        app = Flask(__name__)

        @app.get("/fixtures.ics")
        def fixtures():
            ics = build_ics()
            return Response(ics, mimetype="text/calendar; charset=utf-8")

        print("Serving ICS on http://localhost:8000/fixtures.ics")
        app.run(host="0.0.0.0", port=8000)
    else:
        ics = build_ics()
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(ics)
        print(f"Wrote {args.out}")

if __name__ == "__main__":
    main()
