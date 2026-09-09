# Hawser

Competitive intelligence for the working waterfront. Public AIS traffic in,
a morning brief for towing / marine-services operators out.

Personal project. Clean-room: public API, public docs, original design; no
lineage with any employer tooling.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export AISSTREAM_KEY=...        # free key: https://aisstream.io (GitHub sign-in)

# 1. collect (leave running; 15 min default, --minutes 0 for unbounded)
.venv/bin/python pipeline/collector.py --harbor port_everglades --minutes 0

# 2. see which workboats showed up, map them in pipeline/operators.json
.venv/bin/python pipeline/brief.py --unassigned

# 3. brief (markdown) + site data (json)
.venv/bin/python pipeline/brief.py --days 7 --md brief.md --json docs/data/harbor.json
```

`docs/` is the landing site. If `docs/data/harbor.json` exists it draws real
tracks and operator hours; otherwise it shows the labeled sample animation.

Harbors are bounding boxes in `collector.py` (`HARBORS`). `HAWSER_DB` env var
overrides the SQLite path (default `pipeline/hawser.db`).

## Activity model (v1)

A position sample is *active* at SOG ≥ 1 kn; active samples less than 20 min
apart form a *movement*; movements under 10 min are dropped. Active hours =
sum of movement durations. Movements approximate jobs (assists, escorts,
shifts) — good for share-of-harbor, not a billing record.

## Enrichment (public data)

`pipeline/enrich.py` turns a vessel or company into an identity, from public
sources only — no employer data, no login-gated scraping.

```bash
python pipeline/enrich.py cnpj 16404287000155        # Receita Federal identity
python pipeline/enrich.py owner 9839131              # IMO -> owner (manual cache)
python pipeline/enrich.py census port_everglades 2025 --month 6   # needs CENSUS_KEY
```

- **CNPJ** (Receita Federal via BrasilAPI, free): official Brazilian company
  name, address, activity. Cached in `pipeline/enrich_cache.json`.
- **Owner/manager**: Equasis is public but login-gated and forbids automation,
  so Hawser reads a hand-maintained `pipeline/vessel_owners.json`
  (`{"<imo>": {"owner","manager","cnpj"}}`). Add a `cnpj` and the brief shows
  the resolved company identity automatically.
- **US Census trade**: Brazil→US imports by port for market sizing. Needs a free
  `CENSUS_KEY` (api.census.gov/data/key_signup.html).

The brief flags **Brazil-trade** arrivals (from AIS destination / Brazilian port
history) and shows owner/company identity on deep-draft vessels when known.
The collector also captures IMO numbers and can point at Brazilian load ports
(`--harbor santos|paranagua|rio`) to watch the origin side of the trade.
