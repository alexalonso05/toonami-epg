#!/usr/bin/env python3
"""
Toonami Aftermath -> XMLTV EPG generator.

Pulls upcoming schedule data from the (unofficial/internal) Toonami Aftermath
API and converts it into a standard XMLTV file that IPTV players such as
STZ Player, Jellyfin, TiviMate, etc. can use as a guide-data source.

IMPORTANT: this hits a live "what's on next" endpoint that only returns a
rolling window of upcoming items per channel (not a fixed daily schedule),
so the output is only accurate for the next HOURS_AHEAD hours from the
moment you run it. Re-run this periodically (e.g. via cron, or the included
GitHub Actions workflow) to keep the guide fresh.

Usage:
    python3 generate_epg.py [hours_ahead] [output_path]

Defaults: hours_ahead=48, output_path=epg.xml
"""
import sys
import time
import requests
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
from datetime import datetime, timedelta, timezone

API_URL = "https://api.toonamiaftermath.com/channelsCurrentMedia"

# Maps the API's internal channel "name" -> the tvg-id used in the m3u8 playlist.
# Channels not listed here (e.g. the API's internal "Live Code" test channel)
# are ignored. Channels that are listed but the API returns no items for
# (Radio / Movies / MTV97 tend to have no scheduled-episode data) simply end
# up with an empty guide, which is expected -- not an error.
CHANNEL_ID_MAP = {
    "Toonami Aftermath East": "toonami-east",
    "Toonami Aftermath West": "toonami-west",
    "Movies": "toonami-movies",
    "Toonami Aftermath Radio": "toonami-radio",
    "Snickelodeon East": "snick-east",
    "Snickelodeon West": "snick-west",
    "MTV97": "mtv97",
}

# Some players match EPG channels by exact playlist channel-name text rather
# than tvg-id, so the <display-name> must match the m3u8's display text
# exactly (character for character, including "EST"/"PST" suffixes).
PLAYLIST_DISPLAY_NAME = {
    "toonami-east": "Toonami Aftermath East EST",
    "toonami-west": "Toonami Aftermath West PST",
    "toonami-movies": "Toonami Aftermath Movies",
    "toonami-radio": "Toonami Aftermath Radio",
    "snick-east": "Snickelodeon East EST",
    "snick-west": "Snickelodeon West PST",
    "mtv97": "MTV 97",
}

# Optional per-channel logo for the <channel><icon> element (matches the m3u8's tvg-logo).
CHANNEL_LOGOS = {
    "toonami-east": "https://raw.githubusercontent.com/kbmystery7/TAM3U8/main/ta%20wall%20thumb%20east.jpg",
    "toonami-west": "https://raw.githubusercontent.com/kbmystery7/TAM3U8/main/ta%20wall%20thumb%20west.jpg",
    "toonami-movies": "https://raw.githubusercontent.com/kbmystery7/TAM3U8/main/ta%20wall%20thumb%20movies.jpg",
    "toonami-radio": "https://raw.githubusercontent.com/kbmystery7/TAM3U8/main/ta%20wall%20thumb%20radio.jpg",
}

MAX_ITERATIONS = 40          # hard safety cap on API calls per run
DEFAULT_HOURS_AHEAD = 48
REQUEST_TIMEOUT = 15
FALLBACK_DURATION_MIN = 30   # assumed length for a channel's final captured item


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_xmltv(dt):
    return dt.strftime("%Y%m%d%H%M%S +0000")


def fetch(start_dt):
    resp = requests.get(API_URL, params={"startDate": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")},
                         timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def collect_schedule(hours_ahead):
    horizon = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    cursor = datetime.now(timezone.utc)
    # per api-channel-name -> dict keyed by startDate string -> item (dedup)
    collected = {name: {} for name in CHANNEL_ID_MAP}

    for _ in range(MAX_ITERATIONS):
        try:
            data = fetch(cursor)
        except requests.RequestException as e:
            print(f"  warning: request failed ({e}), stopping early", file=sys.stderr)
            break

        advanced = []
        for ch in data:
            name = ch.get("name")
            if name not in CHANNEL_ID_MAP:
                continue
            for item in ch.get("media", []):
                collected[name][item["startDate"]] = item
            if ch.get("media"):
                advanced.append(parse_iso(ch["media"][-1]["startDate"]))

        if not advanced:
            break  # no channel we care about returned anything new
        new_cursor = max(advanced) + timedelta(seconds=1)
        if new_cursor <= cursor:
            break  # not making progress, avoid infinite loop
        cursor = new_cursor

        # stop once every channel that has EVER returned data has reached the horizon
        active = [name for name, items in collected.items() if items]
        if active and all(
            max(parse_iso(ts) for ts in collected[name]) >= horizon for name in active
        ):
            break
        time.sleep(0.1)  # be polite to the API

    return collected


def build_xmltv(collected):
    tv = ET.Element("tv", {
        "generator-info-name": "toonami-aftermath-epg-generator",
        "generator-info-url": "https://api.toonamiaftermath.com",
    })

    # <channel> elements first (XMLTV convention).
    # Emit the exact playlist display-name FIRST (some players match on the
    # first/only display-name text rather than tvg-id), then the API's own
    # name as a second display-name for players that check all of them.
    for api_name, tvg_id in CHANNEL_ID_MAP.items():
        chan = ET.SubElement(tv, "channel", {"id": tvg_id})
        ET.SubElement(chan, "display-name").text = PLAYLIST_DISPLAY_NAME.get(tvg_id, api_name)
        if PLAYLIST_DISPLAY_NAME.get(tvg_id) != api_name:
            ET.SubElement(chan, "display-name").text = api_name
        logo = CHANNEL_LOGOS.get(tvg_id)
        if logo:
            ET.SubElement(chan, "icon", {"src": logo})

    # <programme> elements
    for api_name, tvg_id in CHANNEL_ID_MAP.items():
        items = [collected[api_name][k] for k in sorted(collected[api_name])]
        for i, media in enumerate(items):
            start_dt = parse_iso(media["startDate"])
            if i + 1 < len(items):
                stop_dt = parse_iso(items[i + 1]["startDate"])
            else:
                stop_dt = start_dt + timedelta(minutes=FALLBACK_DURATION_MIN)

            prog = ET.SubElement(tv, "programme", {
                "start": fmt_xmltv(start_dt),
                "stop": fmt_xmltv(stop_dt),
                "channel": tvg_id,
            })
            info = media.get("info", {})
            title = info.get("fullname") or media.get("name") or "Unknown"
            ET.SubElement(prog, "title", {"lang": "en"}).text = title

            episode = info.get("episode")
            if episode:
                ET.SubElement(prog, "sub-title", {"lang": "en"}).text = episode

            block = media.get("blockName")
            if block:
                ET.SubElement(prog, "category", {"lang": "en"}).text = block

            ep_num = media.get("episodeNumber")
            if ep_num is not None:
                # XMLTV onscreen-style episode numbering
                ET.SubElement(prog, "episode-num", {"system": "onscreen"}).text = str(ep_num)

            image = info.get("image")
            if image:
                ET.SubElement(prog, "icon", {"src": image})

    raw = ET.tostring(tv, encoding="utf-8")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ")
    # strip blank lines minidom likes to add
    return "\n".join(line for line in pretty.splitlines() if line.strip())


def main():
    hours_ahead = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_HOURS_AHEAD
    out_path = sys.argv[2] if len(sys.argv) > 2 else "epg.xml"

    print(f"Collecting ~{hours_ahead}h of schedule data from {API_URL} ...")
    collected = collect_schedule(hours_ahead)
    for name, items in collected.items():
        print(f"  {name}: {len(items)} programme(s)")

    xml_text = build_xmltv(collected)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml_text)
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
