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
    ap.add_argument("--html", metavar="OUT_HTML")
    ap.add_argument("--sample", action="store_true", help="mark HTML output as sample data")
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
    if a.html:
        os.makedirs(os.path.dirname(os.path.abspath(a.html)), exist_ok=True)
        with open(a.html, "w") as f:
            f.write(render_html(b, sample=a.sample))
        print(f"wrote {a.html}")
    if not (a.md or a.json or a.html):
        print(md)




# --- HTML rendering (site brief page) ---------------------------------------

HTML_TMPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hawser brief — {title}</title>
<style>
  :root{{--ink:#0B1220;--panel:#101B2E;--line:rgba(122,183,255,.14);--text:#DCE6F2;--muted:#8CA0B8;--amber:#F5A623;--green:#38E1B0;--mono:"SF Mono",ui-monospace,Menlo,Consolas,monospace}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--ink);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,sans-serif;line-height:1.6}}
  .wrap{{max-width:860px;margin:0 auto;padding:0 24px 80px}}
  a{{color:var(--green);text-decoration:none}}
  header{{padding:26px 0;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between}}
  .brand{{display:flex;align-items:center;gap:12px}}
  .knot{{width:26px;height:26px;border:3px solid var(--amber);border-radius:50%;position:relative}}
  .knot::after{{content:"";position:absolute;inset:5px;border:3px solid var(--amber);border-radius:50%;opacity:.45}}
  .brand b{{font-size:17px;letter-spacing:.14em}}
  .meta{{font-family:var(--mono);font-size:11px;letter-spacing:.12em;color:var(--muted);text-transform:uppercase}}
  .banner{{margin:22px 0 0;border:1px solid var(--amber);color:var(--amber);font-family:var(--mono);font-size:12px;letter-spacing:.14em;padding:10px 14px;text-transform:uppercase}}
  h1{{font-size:30px;margin:38px 0 4px;letter-spacing:-.01em}}
  .sub{{color:var(--muted);margin:0 0 8px}}
  h2{{font-size:15px;font-family:var(--mono);letter-spacing:.18em;text-transform:uppercase;color:var(--amber);margin:44px 0 14px}}
  table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);font-size:14px}}
  th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line)}}
  th{{font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:500}}
  td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}}
  tr:last-child td{{border-bottom:none}}
  .bar{{display:inline-block;height:8px;background:var(--amber);vertical-align:middle;margin-right:8px}}
  .unassigned td{{color:var(--muted)}}
  ul{{color:var(--muted);font-size:14px;padding-left:20px}}
  .note{{color:var(--muted);font-size:13px;margin-top:44px;border-top:1px solid var(--line);padding-top:18px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <a class="brand" href="/"><span class="knot"></span><b>HAWSER</b></a>
    <span class="meta">{meta}</span>
  </header>
  {banner}
  <h1>{title} brief</h1>
  <p class="sub">Last {days} days &middot; who worked, how much, and what came through the port.</p>
  {sections}
  <p class="note">Derived from public AIS movement only. Movements approximate jobs (assists, escorts, shifts) &mdash; a share-of-harbor signal, not a billing record. &middot; <a href="/">hawser.io</a></p>
</div>
</body>
</html>"""


def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_html(b, sample=False):
    title = b["harbor"].replace("_", " ").title()
    parts = []

    if b["operators"]:
        mx = max(o["active_h"] for o in b["operators"]) or 1
        rows = "".join(
            f'<tr{" class=\"unassigned\"" if o["operator"] == UNASSIGNED else ""}>'
            f'<td>{_esc(o["operator"])}</td><td class="num">{o["vessels"]}</td>'
            f'<td class="num">{o["moves"]}</td>'
            f'<td class="num"><span class="bar" style="width:{max(4, int(70 * o["active_h"] / mx))}px"></span>{o["active_h"]}</td>'
            f'<td class="num">{o["share"]}%</td></tr>'
            for o in b["operators"])
        parts.append('<h2>Who worked</h2><table><tr><th>Operator</th><th class="num">Boats</th>'
                     '<th class="num">Movements</th><th class="num">Active hrs</th><th class="num">Share</th></tr>'
                     + rows + "</table>")

    if b["workboats"]:
        rows = "".join(
            f'<tr{" class=\"unassigned\"" if w["operator"] == UNASSIGNED else ""}>'
            f'<td>{_esc(w["name"])}</td><td>{_esc(w["operator"])}</td>'
            f'<td class="num">{w["moves"]}</td><td class="num">{w["active_h"]}</td>'
            f'<td class="num">{w["days_active"]}</td><td>{w["last_seen"]}</td></tr>'
            for w in b["workboats"])
        parts.append('<h2>By boat</h2><table><tr><th>Boat</th><th>Operator</th><th class="num">Movements</th>'
                     '<th class="num">Active hrs</th><th class="num">Days</th><th>Last seen</th></tr>'
                     + rows + "</table>")

    if b["arrivals_by_day"]:
        classes = sorted({c for d in b["arrivals_by_day"].values() for c in d})
        head = "".join(f'<th class="num">{c}</th>' for c in classes)
        rows = "".join(
            "<tr><td>" + day + "</td>" + "".join(f'<td class="num">{c.get(k, 0)}</td>' for k in classes) + "</tr>"
            for day, c in b["arrivals_by_day"].items())
        ships = "".join(
            f'<li>{_esc(d["name"])} ({d["class"]}) &rarr; {_esc(d["dest"] or "?")} &middot; first seen {d["first_seen"]}</li>'
            for d in b["deep_draft"][-12:])
        parts.append(f'<h2>Where the ships are going</h2><table><tr><th>Day</th>{head}</tr>{rows}</table><ul>{ships}</ul>')

    banner = ('<div class="banner">Sample brief &mdash; synthetic data, real format. '
              'Pilot briefs run on your port&rsquo;s live feed.</div>' if sample else "")
    meta = ("SAMPLE" if sample else "LIVE") + f' &middot; {b["generated"][:16]}Z'
    return HTML_TMPL.format(title=_esc(title), meta=meta, banner=banner,
                            days=b["window_days"], sections="\n".join(parts))


if __name__ == "__main__":
    main()
