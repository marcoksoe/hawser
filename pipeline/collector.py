"""
Hawser AIS Collector — clean-room build, Jul 2026.

Streams public AIS traffic from aisstream.io for a configured harbor,
stores position reports in SQLite, and maintains per-operator activity
rollups that power the Hawser brief.

Setup:
    pip install websockets
    export AISSTREAM_KEY=...   (free key: https://aisstream.io — GitHub sign-in)
    python collector.py --harbor port_everglades --minutes 15

This is Hawser (personal project) code. It shares no lineage with any
employer tooling: public API, public docs, original design.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

try:
    import websockets
except ImportError:
    sys.exit("pip install websockets")

STREAM_URL = "wss://stream.aisstream.io/v0/stream"
DB_PATH = os.environ.get("HAWSER_DB") or os.path.join(os.path.dirname(__file__), "hawser.db")

# Harbor definitions: name -> bounding box [[lat_min, lon_min], [lat_max, lon_max]]
# Coordinates are public chart knowledge.
HARBORS = {
    "port_everglades": [[26.05, -80.15], [26.13, -80.09]],
    "miami": [[25.75, -80.20], [25.79, -80.13]],
    "savannah": [[32.00, -81.15], [32.15, -80.98]],
}

# AIS ship-type codes for the working waterfront (ITU-R M.1371):
# 31/32 = towing, 52 = tug, 50 = pilot, 51 = SAR, 53 = port tender, 33 = dredger
WORKBOAT_TYPES = {31, 32, 33, 50, 51, 52, 53}


def db_init():
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS positions (
            ts_utc      TEXT NOT NULL,
            mmsi        INTEGER NOT NULL,
            name        TEXT,
            ship_type   INTEGER,
            lat         REAL,
            lon         REAL,
            sog         REAL,
            cog         REAL,
            heading     REAL,
            harbor      TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_positions_mmsi_ts ON positions (mmsi, ts_utc);
        CREATE TABLE IF NOT EXISTS vessels (
            mmsi        INTEGER PRIMARY KEY,
            name        TEXT,
            ship_type   INTEGER,
            callsign    TEXT,
            dest        TEXT,
            last_seen   TEXT
        );
        """
    )
    con.commit()
    return con


def upsert_vessel(con, mmsi, name, ship_type, callsign, dest, ts):
    con.execute(
        """
        INSERT INTO vessels (mmsi, name, ship_type, callsign, dest, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(mmsi) DO UPDATE SET
            name      = COALESCE(NULLIF(excluded.name, ''), vessels.name),
            ship_type = COALESCE(excluded.ship_type, vessels.ship_type),
            callsign  = COALESCE(NULLIF(excluded.callsign, ''), vessels.callsign),
            dest      = COALESCE(NULLIF(excluded.dest, ''), vessels.dest),
            last_seen = excluded.last_seen
        """,
        (mmsi, name, ship_type, callsign, dest, ts),
    )


async def run(harbor: str, minutes: int, key: str):
    bbox = HARBORS[harbor]
    con = db_init()
    started = datetime.now(timezone.utc)
    n = 0

    sub = {
        "APIKey": key,
        "BoundingBoxes": [bbox],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }

    print(f"hawser: streaming {harbor} for {minutes or '∞'} min")
    async with websockets.connect(STREAM_URL) as ws:
        await ws.send(json.dumps(sub))
        async for raw in ws:
            if minutes and (datetime.now(timezone.utc) - started).total_seconds() > minutes * 60:
                break
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                continue

            meta = m.get("MetaData", {})
            ts = meta.get("time_utc") or datetime.now(timezone.utc).isoformat()
            mmsi = meta.get("MMSI")
            if not mmsi:
                continue
            name = (meta.get("ShipName") or "").strip()

            if m.get("MessageType") == "PositionReport":
                pr = m.get("Message", {}).get("PositionReport", {})
                con.execute(
                    "INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        ts, mmsi, name, meta.get("ShipType"),
                        pr.get("Latitude"), pr.get("Longitude"),
                        pr.get("Sog"), pr.get("Cog"), pr.get("TrueHeading"),
                        harbor,
                    ),
                )
                upsert_vessel(con, mmsi, name, meta.get("ShipType"), "", "", ts)
            elif m.get("MessageType") == "ShipStaticData":
                sd = m.get("Message", {}).get("ShipStaticData", {})
                upsert_vessel(
                    con, mmsi, name, sd.get("Type"),
                    (sd.get("CallSign") or "").strip(),
                    (sd.get("Destination") or "").strip(), ts,
                )

            n += 1
            if n % 25 == 0:
                con.commit()
                print(f"  {n} messages | last: {name or mmsi}")

    con.commit()
    print(f"done: {n} messages -> {DB_PATH}")


def export_demo_json(out_path: str, harbor: str = "port_everglades", hours: int = 24):
    """Export recent workboat tracks as JSON for the site demo."""
    con = db_init()
    rows = con.execute(
        """
        SELECT p.mmsi, COALESCE(v.name, 'MMSI ' || p.mmsi) AS name,
               COALESCE(v.ship_type, p.ship_type) AS ship_type,
               p.ts_utc, p.lat, p.lon, p.sog
        FROM positions p LEFT JOIN vessels v USING (mmsi)
        WHERE p.harbor = ? AND p.ts_utc >= datetime('now', ?)
        ORDER BY p.mmsi, p.ts_utc
        """,
        (harbor, f"-{hours} hours"),
    ).fetchall()

    tracks = {}
    for mmsi, name, stype, ts, lat, lon, sog in rows:
        if stype is not None and int(stype) not in WORKBOAT_TYPES:
            continue
        tracks.setdefault(mmsi, {"name": name, "type": stype, "points": []})
        tracks[mmsi]["points"].append({"t": ts, "lat": lat, "lon": lon, "sog": sog})

    with open(out_path, "w") as f:
        json.dump({"harbor": harbor, "generated": datetime.now(timezone.utc).isoformat(),
                   "live": True, "tracks": list(tracks.values())}, f)
    print(f"exported {len(tracks)} workboat tracks -> {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--harbor", default="port_everglades", choices=HARBORS)
    ap.add_argument("--minutes", type=int, default=15)
    ap.add_argument("--export-demo", metavar="OUT_JSON")
    a = ap.parse_args()

    if a.export_demo:
        export_demo_json(a.export_demo, a.harbor)
    else:
        key = os.environ.get("AISSTREAM_KEY", "")
        if not key:
            sys.exit("Set AISSTREAM_KEY env var (free key at https://aisstream.io)")
        asyncio.run(run(a.harbor, a.minutes, key))
