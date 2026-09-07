"""
Hawser Brief — turns collected AIS positions into the morning brief.

    python brief.py --harbor port_everglades --days 7            # markdown to stdout
    python brief.py --harbor port_everglades --days 7 --md brief.md --json ../docs/data/harbor.json
    python brief.py --unassigned                                  # workboats with no operator yet

Reads hawser.db (written by collector.py) and operators.json (your MMSI/name
-> operator map). Public AIS data only; original design.

Activity model (v1, deliberately simple):
  * a position sample is "active" when speed over ground >= ACTIVE_KN
  * consecutive active samples closer than GAP_MIN minutes form a movement
  * a movement counts if it lasts >= MIN_MOVE_MIN minutes
  * active hours = sum of movement durations
Movements are a proxy for jobs (assists, escorts, shifts). Good enough to
see who worked and how much; not a billing record.
"""

import argparse
import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from collector import DB_PATH, HARBORS, WORKBOAT_TYPES, db_init

OPERATORS_PATH = os.path.join(os.path.dirname(__file__), "operators.json")

ACTIVE_KN = 1.0
GAP_MIN = 20
MIN_MOVE_MIN = 10
UNASSIGNED = "Unassigned"

# Deep-draft classes for the port-demand section (ITU-R M.1371 first digit).
SHIP_CLASSES = {6: "Passenger", 7: "Cargo", 8: "Tanker"}


def parse_ts(ts: str) -> datetime:
    # aisstream time_utc looks like "2026-07-19 23:00:13.123456789 +0000 UTC";
    # our fallback timestamps are ISO. First 19 chars are the same shape in both.
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.fromisoformat(ts).astimezone(timezone.utc)


def load_operators(path=OPERATORS_PATH):
    if not os.path.exists(path):
        return {}, []
    with open(path) as f:
        cfg = json.load(f)
    by_mmsi = {int(k): v for k, v in cfg.get("mmsi", {}).items()}
    patterns = [
        (op, re.compile(p, re.I))
        for op, pats in cfg.get("name_patterns", {}).items()
        for p in pats
    ]
    return by_mmsi, patterns


def operator_for(mmsi, name, by_mmsi, patterns):
    if mmsi in by_mmsi:
        return by_mmsi[mmsi]
    for op, rx in patterns:
        if name and rx.search(name):
            return op
    return UNASSIGNED


def movements(samples):
    """samples: list of (dt, sog) sorted by dt -> list of (start, end)."""
    out, start, last = [], None, None
    for dt, sog in samples:
        active = sog is not None and sog >= ACTIVE_KN
        if active:
            if start is None or (dt - last) > timedelta(minutes=GAP_MIN):
                if start is not None and (last - start) >= timedelta(minutes=MIN_MOVE_MIN):
                    out.append((start, last))
                start = dt
            last = dt
        elif start is not None:
            if (last - start) >= timedelta(minutes=MIN_MOVE_MIN):
                out.append((start, last))
            start = None
    if start is not None and (last - start) >= timedelta(minutes=MIN_MOVE_MIN):
        out.append((start, last))
    return out


def load_window(con, harbor, days):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = con.execute(
        """
        SELECT p.mmsi, COALESCE(NULLIF(v.name,''), NULLIF(p.name,''), 'MMSI ' || p.mmsi),
               COALESCE(v.ship_type, p.ship_type), v.dest,
               p.ts_utc, p.lat, p.lon, p.sog
        FROM positions p LEFT JOIN vessels v USING (mmsi)
        WHERE p.harbor = ? AND p.ts_utc >= ?
        ORDER BY p.mmsi, p.ts_utc
        """,
        (harbor, since.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    vessels = {}
    for mmsi, name, stype, dest, ts, lat, lon, sog in rows:
        v = vessels.setdefault(mmsi, {"mmsi": mmsi, "name": name, "type": stype, "dest": dest, "pts": []})
        v["pts"].append((parse_ts(ts), lat, lon, sog))
    return since, vessels


def build(harbor, days):
    con = db_init()
    since, vessels = load_window(con, harbor, days)
    by_mmsi, patterns = load_operators()

    ops = defaultdict(lambda: {"vessels": {}, "moves": 0, "active_h": 0.0, "by_day": defaultdict(float)})
    workboats, deep = [], []

    for v in vessels.values():
        stype = v["type"]
        if stype is not None and int(stype) in WORKBOAT_TYPES:
            mv = movements([(dt, sog) for dt, _, _, sog in v["pts"]])
            hours = sum((e - s).total_seconds() for s, e in mv) / 3600
            op = operator_for(v["mmsi"], v["name"], by_mmsi, patterns)
            rec = {"mmsi": v["mmsi"], "name": v["name"], "type": stype, "operator": op,
                   "moves": len(mv), "active_h": round(hours, 1),
                   "days_active": len({s.date() for s, _ in mv}),
                   "last_seen": max(dt for dt, *_ in v["pts"]).strftime("%Y-%m-%d %H:%M"),
                   "track": [{"t": dt.isoformat(), "lat": la, "lon": lo, "sog": sog}
                             for dt, la, lo, sog in v["pts"] if la is not None]}
            workboats.append(rec)
            o = ops[op]
            o["vessels"][v["mmsi"]] = rec["name"]
            o["moves"] += len(mv)
            o["active_h"] += hours
            for s, e in mv:
                o["by_day"][s.date().isoformat()] += (e - s).total_seconds() / 3600
        elif stype is not None and int(stype) // 10 in SHIP_CLASSES:
            first = min(dt for dt, *_ in v["pts"])
            deep.append({"mmsi": v["mmsi"], "name": v["name"], "class": SHIP_CLASSES[int(stype) // 10],
                         "dest": v["dest"] or "", "first_seen": first.strftime("%Y-%m-%d %H:%M")})

    total_h = sum(o["active_h"] for o in ops.values()) or 1.0
    op_rows = sorted(
        ({"operator": op, "vessels": len(o["vessels"]), "moves": o["moves"],
          "active_h": round(o["active_h"], 1), "share": round(100 * o["active_h"] / total_h),
          "by_day": dict(sorted(o["by_day"].items()))}
         for op, o in ops.items()),
        key=lambda r: -r["active_h"],
    )
    arrivals = defaultdict(lambda: defaultdict(int))
    for d in deep:
        arrivals[d["first_seen"][:10]][d["class"]] += 1

    return {
        "harbor": harbor, "bbox": HARBORS[harbor], "generated": datetime.now(timezone.utc).isoformat(),
        "window_days": days, "since": since.isoformat(), "live": True,
        "operators": op_rows,
        "workboats": sorted(workboats, key=lambda w: (-w["active_h"], w["name"])),
        "deep_draft": sorted(deep, key=lambda d: d["first_seen"]),
        "arrivals_by_day": {d: dict(c) for d, c in sorted(arrivals.items())},
    }


def render_md(b):
    L = []
    title = b["harbor"].replace("_", " ").title()
    L += [f"# Hawser brief — {title}", "",
          f"Window: last {b['window_days']} days · generated {b['generated'][:16]}Z · public AIS only", ""]

    L += ["## Who worked", ""]
    if b["operators"]:
        L += ["| Operator | Boats seen | Movements | Active hrs | Share |", "|---|---:|---:|---:|---:|"]
        L += [f"| {o['operator']} | {o['vessels']} | {o['moves']} | {o['active_h']} | {o['share']}% |"
              for o in b["operators"]]
    else:
        L.append("_No workboat activity in window._")
    L.append("")

    L += ["## By boat", ""]
    if b["workboats"]:
        L += ["| Boat | Operator | Movements | Active hrs | Days active | Last seen |", "|---|---|---:|---:|---:|---|"]
        L += [f"| {w['name']} | {w['operator']} | {w['moves']} | {w['active_h']} | {w['days_active']} | {w['last_seen']} |"
              for w in b["workboats"]]
    L.append("")

    L += ["## Where the ships are going", ""]
    if b["arrivals_by_day"]:
        classes = sorted({c for d in b["arrivals_by_day"].values() for c in d})
        L += ["| Day | " + " | ".join(classes) + " |", "|---|" + "---:|" * len(classes)]
        L += ["| " + day + " | " + " | ".join(str(c.get(k, 0)) for k in classes) + " |"
              for day, c in b["arrivals_by_day"].items()]
        L.append("")
        L += [f"- {d['name']} ({d['class']}) → {d['dest'] or '?'} · first seen {d['first_seen']}"
              for d in b["deep_draft"][-15:]]
    else:
        L.append("_No deep-draft arrivals in window._")
    L.append("")

    un = [w for w in b["workboats"] if w["operator"] == UNASSIGNED]
    if un:
        L += ["## Unassigned workboats (add to operators.json)", ""]
        L += [f"- {w['mmsi']} · {w['name']} · type {w['type']} · {w['active_h']} h" for w in un]
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--harbor", default="port_everglades", choices=HARBORS)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--md", metavar="OUT_MD")
    ap.add_argument("--json", metavar="OUT_JSON")
    ap.add_argument("--unassigned", action="store_true", help="only list workboats with no operator")
    a = ap.parse_args()

    b = build(a.harbor, a.days)
    if a.unassigned:
        for w in b["workboats"]:
            if w["operator"] == UNASSIGNED:
                print(f"{w['mmsi']}\t{w['name']}\ttype {w['type']}\t{w['active_h']} h")
        return
    md = render_md(b)
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, "w") as f:
            json.dump(b, f)
        print(f"wrote {a.json}")
    if a.md:
        with open(a.md, "w") as f:
            f.write(md)
        print(f"wrote {a.md}")
    if not (a.md or a.json):
        print(md)


if __name__ == "__main__":
    main()
