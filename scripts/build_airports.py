"""Build app/static/airports.json from OurAirports (public domain, ourairports.com/data).

    python scripts/build_airports.py [path/to/ourairports-data]

Without a path the three CSVs are downloaded from the ourairports-data GitHub mirror.
Keeps airports with an IATA code and scheduled passenger service. Output:
    {"DFW": ["Dallas-Fort Worth", "TX", "Dallas Fort Worth International Airport", 1], ...}
city, region (US/Canadian state or province code, else the country name), airport name, and
1 if Southwest flies there (listed first in autocomplete).
"""
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

SRC = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/{}.csv"
OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "airports.json"

# Southwest's network (domestic and international), for ordering suggestions only: any
# airport code can still be entered.
SOUTHWEST = set("""
ABQ ALB AMA ATL AUA AUS BDL BHM BNA BOI BOS BUF BUR BWI BZE BZN CHS CLE CLT CMH CRP CUN CVG
DAL DCA DEN DSM DTW ECP ELP EUG EYW FAT FLL GCM GEG GRR GSP HDN HNL HOU IAD ICT IND ISP ITO
JAN JAX KOA LAS LAX LBB LGA LGB LIH LIR LIT MAF MBJ MCI MCO MDW MEM MHT MIA MKE MRY MSP MSY
MTJ MYR NAS OAK OGG OKC OMA ONT ORF PBI PDX PHL PHX PIT PNS PSP PUJ PVD PVR PWM RDU RIC RNO
ROC RSW SAN SAT SAV SBA SDF SEA SFO SJC SJD SJO SJU SLC SMF SNA SRQ STL STT TPA TUL TUS VPS
""".split())


def read(name, base):
    if base:
        return list(csv.DictReader(open(Path(base) / f"{name}.csv", encoding="utf-8")))
    with urllib.request.urlopen(SRC.format(name), timeout=60) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))


def main(base=None):
    countries = {c["code"]: c["name"] for c in read("countries", base)}
    out = {}
    for a in read("airports", base):
        code = (a["iata_code"] or "").strip().upper()
        if len(code) != 3 or not code.isalpha() or a["scheduled_service"] != "yes":
            continue
        if a["type"] not in ("large_airport", "medium_airport", "small_airport"):
            continue
        country = a["iso_country"]
        region = a["iso_region"].split("-", 1)[-1] if country in ("US", "CA") else countries.get(country, country)
        entry = [a["municipality"] or a["name"], region, a["name"], int(code in SOUTHWEST)]
        # several rows can share a code: prefer the bigger airport
        rank = ("small_airport", "medium_airport", "large_airport").index(a["type"])
        if code not in out or rank > out[code][1]:
            out[code] = (entry, rank)
    # a Southwest code an airport has since been renamed from (listed among its keywords,
    # e.g. PBI) still resolves, since that's the code people and Southwest may use
    by_name = {v[0][2]: v[0] for v in out.values()}
    for a in read("airports", base) if SOUTHWEST - out.keys() else []:
        keys = {k.strip().upper() for k in (a["keywords"] or "").split(",")}
        for code in (SOUTHWEST - out.keys()) & keys:
            if a["name"] in by_name:
                out[code] = ([*by_name[a["name"]][:3], 1], 0)
    data = {k: v[0] for k, v in sorted(out.items())}
    missing = sorted(SOUTHWEST - data.keys())
    if missing:
        print("warning: Southwest airports not found:", " ".join(missing), file=sys.stderr)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(data)} airports ({sum(v[3] for v in data.values())} Southwest) -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
