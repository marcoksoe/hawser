"""
Hawser enrichment — turn a vessel/company into an identity, from public data.

Clean-room: public, citable sources only. No employer data, no login-gated
scraping. Where a source needs an account (Equasis), Hawser reads a
hand-maintained cache you populate; it never automates someone's login.

Sources
  * Receita Federal CNPJ (via BrasilAPI, free public mirror) — official
    Brazilian company identity: legal name, address, activity codes.
  * Equasis (IMO -> registered owner / ISM manager) — MANUAL cache only.
  * US Census International Trade API — Brazil->US imports by port/commodity,
    for market sizing. Needs a free CENSUS_KEY (api.census.gov/data/key_signup.html).

    from enrich import cnpj_lookup, owner_for_imo, census_brazil_imports
    cnpj_lookup("60.398.138/0001-60")     # Citrosuco S/A, e.g.
    owner_for_imo(9839131)                 # -> {"owner":..., "manager":..., "cnpj":...}
    census_brazil_imports("Port Everglades", "2026")
"""

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

_DIR = os.path.dirname(__file__)
CACHE_PATH = os.path.join(_DIR, "enrich_cache.json")
OWNERS_PATH = os.path.join(_DIR, "vessel_owners.json")
UA = {"User-Agent": "hawser/enrich (+https://hawser.io)"}
TIMEOUT = 15


def _cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _cache_put(kind, key, value):
    c = _cache()
    c.setdefault(kind, {})[str(key)] = value
    with open(CACHE_PATH, "w") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)


def _get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


# --- Receita Federal CNPJ (public company registry) -------------------------

def cnpj_lookup(cnpj, use_cache=True):
    """Official identity for a Brazilian company. Returns a dict or None."""
    digits = re.sub(r"\D", "", str(cnpj))
    if len(digits) != 14:
        return None
    cache = _cache()
    if use_cache and digits in cache.get("cnpj", {}):
        return cache["cnpj"][digits]
    try:
        d = _get_json(f"https://brasilapi.com.br/api/cnpj/v1/{digits}")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None
    rec = {
        "cnpj": digits,
        "name": d.get("razao_social") or d.get("nome_fantasia"),
        "trade_name": d.get("nome_fantasia"),
        "city": d.get("municipio"),
        "state": d.get("uf"),
        "activity": d.get("cnae_fiscal_descricao"),
        "activity_code": d.get("cnae_fiscal"),
    }
    _cache_put("cnpj", digits, rec)
    return rec


# --- Equasis owner/manager (manual cache; login-gated source) ---------------

def owner_for_imo(imo):
    """Registered owner / ISM manager for an IMO, from the manual cache.

    Populate pipeline/vessel_owners.json yourself (or paste Equasis lookups):
        { "9839131": {"owner": "...", "manager": "...", "cnpj": "..."} }
    Returns the record (with CNPJ identity merged in if present) or None.
    Never scrapes Equasis — it requires a login and its ToS forbids automation.
    """
    if not imo:
        return None
    if not os.path.exists(OWNERS_PATH):
        return None
    try:
        with open(OWNERS_PATH) as f:
            owners = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    rec = owners.get(str(imo))
    if not rec:
        return None
    out = dict(rec)
    if rec.get("cnpj"):
        ident = cnpj_lookup(rec["cnpj"])
        if ident:
            out["company"] = ident
    return out


# --- US Census trade (market sizing) ----------------------------------------

# Census district codes for the ports Hawser watches (Schedule D).
CENSUS_DISTRICTS = {
    "port_everglades": "5201", "miami": "5201", "savannah": "1703",
}
BRAZIL_CTY = "3510"  # Schedule C country code, Brazil


def census_brazil_imports(harbor, year, month=None, key=None):
    """General imports (customs value, USD) from Brazil through a port's
    Census district for a period. Returns a dict or None. Public API."""
    district = CENSUS_DISTRICTS.get(harbor)
    if not district:
        return None
    key = key or os.environ.get("CENSUS_KEY", "")
    if not key:
        return None  # Census requires a key; no keyless access.
    params = {
        "get": "GEN_VAL_MO,CTY_NAME,DIST_NAME",
        "CTY_CODE": BRAZIL_CTY,
        "DIST_CODE": district,
        "time": f"{year}-{month:02d}" if month else year,
    }
    if key:
        params["key"] = key
    url = "https://api.census.gov/data/timeseries/intltrade/imports/porths?" + urllib.parse.urlencode(params)
    try:
        rows = _get_json(url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None
    if not rows or len(rows) < 2:
        return None
    hdr = rows[0]
    val_i = hdr.index("GEN_VAL_MO") if "GEN_VAL_MO" in hdr else 0
    total = sum(int(r[val_i]) for r in rows[1:] if r[val_i] and r[val_i].lstrip("-").isdigit())
    return {"harbor": harbor, "period": params["time"], "brazil_import_usd": total,
            "district": district}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Hawser public-data enrichment")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("cnpj"); c.add_argument("cnpj")
    o = sub.add_parser("owner"); o.add_argument("imo", type=int)
    ce = sub.add_parser("census"); ce.add_argument("harbor"); ce.add_argument("year"); ce.add_argument("--month", type=int)
    a = ap.parse_args()
    if a.cmd == "cnpj":
        print(json.dumps(cnpj_lookup(a.cnpj), indent=2, ensure_ascii=False))
    elif a.cmd == "owner":
        print(json.dumps(owner_for_imo(a.imo), indent=2, ensure_ascii=False))
    elif a.cmd == "census":
        print(json.dumps(census_brazil_imports(a.harbor, a.year, a.month), indent=2))
    else:
        ap.print_help()
