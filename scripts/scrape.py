"""
Scraper for Minus møbelverdi-verktøy.
Henter bygningsdata fra selskapers nettsider.
Oppdager nye store eiendomsselskaper via Brreg + Regnskapsregisteret.
Genererer oppdatert index.html.
"""

import requests
from bs4 import BeautifulSoup
import re
import json
import time
import warnings
from datetime import date
from pathlib import Path

warnings.filterwarnings("ignore")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
}
RATE = 7353
THIS_YEAR = date.today().year
MIN_KVM = 100_000          # Nedre grense for porteføljesstørrelse
MIN_OMSETNING_NOK = 50_000_000  # Proxy: ~50 MNOK leieinntekter ≈ 50 000+ kvm

# ---------------------------------------------------------------------------
# Brreg: finn store norske eiendomsselskaper
# ---------------------------------------------------------------------------

EIENDOM_KODER = ["68.100", "68.201", "68.209", "68.320"]

def brreg_finn_kandidater(min_omsetning=MIN_OMSETNING_NOK, maks_kandidater=50):
    """
    Henter eiendomsselskaper fra Brreg og filtrerer på omsetning fra
    Regnskapsregisteret som proxy for porteføljestørrelse.
    Returnerer liste med {navn, orgnr, omsetning, hjemmeside}.
    """
    print("Søker i Brreg etter store eiendomsselskaper...")
    kandidater = {}

    for kode in EIENDOM_KODER:
        side = 0
        while True:
            try:
                r = requests.get(
                    "https://data.brreg.no/enhetsregisteret/api/enheter",
                    params={"naeringskode": kode, "size": 100, "page": side},
                    headers={"Accept": "application/json"},
                    timeout=10,
                )
                if r.status_code != 200:
                    break
                data = r.json()
                enheter = data.get("_embedded", {}).get("enheter", [])
                if not enheter:
                    break

                for e in enheter:
                    orgnr = e.get("organisasjonsnummer")
                    if orgnr and orgnr not in kandidater:
                        kandidater[orgnr] = {
                            "navn": e.get("navn", ""),
                            "orgnr": orgnr,
                            "hjemmeside": e.get("hjemmeside") or "",
                        }

                # Stopp hvis vi har nok
                if data.get("page", {}).get("totalPages", 1) <= side + 1:
                    break
                side += 1
                time.sleep(0.1)
            except Exception:
                break

    print(f"  Fant {len(kandidater)} selskaper totalt — filtrerer på omsetning ≥ {min_omsetning/1e6:.0f} MNOK")

    # Filtrer via Regnskapsregisteret
    store = []
    for orgnr, info in list(kandidater.items()):
        omsetning = _hent_omsetning(orgnr)
        if omsetning and omsetning >= min_omsetning:
            info["omsetning"] = omsetning
            store.append(info)
        time.sleep(0.05)

    store.sort(key=lambda x: x["omsetning"], reverse=True)
    print(f"  {len(store)} selskaper over terskel")
    return store[:maks_kandidater]


def _hent_omsetning(orgnr):
    """Henter siste tilgjengelige omsetning fra Regnskapsregisteret."""
    try:
        r = requests.get(
            f"https://data.brreg.no/regnskapsregisteret/regnskap/{orgnr}",
            headers={"Accept": "application/json"},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        regnskaper = r.json()
        if not regnskaper:
            return None
        # Siste regnskap først
        siste = sorted(regnskaper, key=lambda x: x.get("regnskapsperiode", {}).get("fraDato", ""), reverse=True)[0]
        return siste.get("resultatregnskapResultat", {}).get("driftsresultat", {}).get("driftsinntekter", {}).get("sumDriftsinntekter")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Finn hjemmeside for selskap via Brreg
# ---------------------------------------------------------------------------

def finn_hjemmeside(orgnr, navn):
    """Prøver å finne selskapets hjemmeside."""
    try:
        r = requests.get(
            f"https://data.brreg.no/enhetsregisteret/api/enheter/{orgnr}",
            headers={"Accept": "application/json"},
            timeout=8,
        )
        if r.status_code == 200:
            hjemmeside = r.json().get("hjemmeside")
            if hjemmeside:
                if not hjemmeside.startswith("http"):
                    hjemmeside = "https://" + hjemmeside
                return hjemmeside
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Generisk website-scraper
# ---------------------------------------------------------------------------

def scrape_nettsted(navn, url, min_kvm=MIN_KVM):
    """
    Forsøker å hente bygg, kvm og byggeår fra et eiendomsselskaps nettsted.
    Returnerer liste med bygg-dicts, eller tom liste hvis ikke nok data.
    """
    if not url:
        return []
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, verify=False)
        soup = BeautifulSoup(r.text, "lxml")
        raw = soup.get_text()

        # Finn alle interne lenker som kan være eiendomssider
        base = "/".join(url.split("/")[:3])
        prop_links = list(set([
            (base + a["href"]) if a["href"].startswith("/") else a["href"]
            for a in soup.find_all("a", href=True)
            if any(k in a["href"].lower() for k in ["eiendom", "bygg", "propert", "portfolio"])
            and "http" in (a["href"] if a["href"].startswith("http") else base + a["href"])
        ]))

        results = []
        for link in prop_links[:30]:
            try:
                pr = requests.get(link, headers=HEADERS, timeout=10, verify=False)
                psoup = BeautifulSoup(pr.text, "lxml")
                praw = psoup.get_text()

                kvm = _parse_kvm(praw)
                yr = _parse_year(praw)
                if not kvm or kvm < 500:
                    continue

                h1 = psoup.find("h1")
                pnavn = h1.get_text(strip=True).split(":")[0][:80] if h1 else link.split("/")[-1]
                by = _city_from_text(praw)

                results.append({"n": pnavn, "by": by, "kvm": kvm, "ma": 4, "yr": yr, "s": status(yr)})
                time.sleep(0.15)
            except Exception:
                pass

        total_kvm = sum(r["kvm"] for r in results)
        if total_kvm < min_kvm:
            return []
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Kjente scrapere med tilpasset logikk
# ---------------------------------------------------------------------------

def scrape_entra():
    print("Scraper Entra...")
    r = requests.get("https://www.entra.no/sitemap.xml", headers=HEADERS, timeout=10, verify=False)
    urls = re.findall(
        r"<loc>(https://www\.entra\.no/vare-eiendommer/alle-eiendommer/[^<]+)</loc>", r.text
    )
    results = []
    for url in urls:
        try:
            pr = requests.get(url, headers=HEADERS, timeout=10, verify=False)
            soup = BeautifulSoup(pr.text, "lxml")
            raw = soup.get_text()

            h1 = soup.find("h1")
            name = h1.get_text(strip=True).split(":")[0].strip() if h1 else url.split("/")[-1]

            kvm_m = re.search(r"Størrelse\s*([\d \xa0\s]+?)(?:\n|\s)*kvm", raw)
            kvm = None
            if kvm_m:
                try:
                    kvm = int(kvm_m.group(1).replace(" ", "").replace("\xa0", "").replace("\n", "").strip())
                except Exception:
                    pass

            yr_m = re.search(r"Bygge[åa]r\s*(\d{4})", raw)
            yr = int(yr_m.group(1)) if yr_m else None

            area_m = re.search(r"Område\s*([^\n]+)", raw)
            by_raw = area_m.group(1).strip() if area_m else "Oslo"
            by = _city(by_raw)

            if kvm and kvm >= 800:
                results.append({"n": name, "by": by, "kvm": kvm, "ma": 4, "yr": yr, "s": status(yr)})
            time.sleep(0.15)
        except Exception:
            pass

    total_kvm = sum(r["kvm"] for r in results)
    print(f"  Entra: {len(results)} bygg | {total_kvm:,} kvm")
    return results if total_kvm >= MIN_KVM else []


def scrape_klp():
    print("Scraper KLP Eiendom (hardkodet)...")
    return [
        {"n":"Frydenlund (Pilestredet 40-52)","by":"Oslo","kvm":82320,"ma":4,"yr":2010,"s":"nå"},
        {"n":"Trondheimsveien 2 (Schous)","by":"Oslo","kvm":54000,"ma":4,"yr":2018,"s":"nå"},
        {"n":"Drammensveien 288","by":"Lysaker","kvm":53970,"ma":4,"yr":2020,"s":"snart"},
        {"n":"Biskop Gunnerusgate 14","by":"Oslo","kvm":52600,"ma":5,"yr":None,"s":"ingen"},
        {"n":"Akersgata 55","by":"Oslo","kvm":34500,"ma":4,"yr":2016,"s":"nå"},
        {"n":"Ibsenkvartalet","by":"Oslo","kvm":33000,"ma":4,"yr":2015,"s":"nå"},
        {"n":"Stortorvet 7","by":"Oslo","kvm":32700,"ma":4,"yr":2023,"s":"ok"},
        {"n":"Schweigaardsgate 17-19","by":"Oslo","kvm":25000,"ma":4,"yr":2013,"s":"nå"},
        {"n":"Dronning Eufemiasgate 10","by":"Oslo","kvm":25800,"ma":4,"yr":2012,"s":"nå"},
        {"n":"Pilestredet 75C","by":"Oslo","kvm":24970,"ma":4,"yr":2016,"s":"nå"},
        {"n":"Holbergs Terrasse","by":"Oslo","kvm":24670,"ma":4,"yr":2020,"s":"snart"},
        {"n":"Eufemia (Dr. Eufemiasgate 71)","by":"Oslo","kvm":22422,"ma":4,"yr":2018,"s":"snart"},
        {"n":"Wergelandsveien 1-3","by":"Oslo","kvm":21360,"ma":3,"yr":2015,"s":"nå"},
        {"n":"Teknostallen (Teknobyen)","by":"Trondheim","kvm":36636,"ma":5,"yr":2024,"s":"ok"},
        {"n":"Trondheim bygg 20 186 kvm","by":"Trondheim","kvm":20186,"ma":4,"yr":2015,"s":"nå"},
        {"n":"Brøsetvegen 164 (Lyngården)","by":"Trondheim","kvm":19400,"ma":4,"yr":2015,"s":"nå"},
        {"n":"Akersgata 64/68","by":"Oslo","kvm":20500,"ma":4,"yr":2016,"s":"nå"},
        {"n":"Jonsvollskvartalet","by":"Bergen","kvm":19400,"ma":5,"yr":2020,"s":"snart"},
        {"n":"Klæbuveien 72","by":"Trondheim","kvm":17706,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Lysaker Torg 45","by":"Lysaker","kvm":17152,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Trondheim bygg 17 222 kvm","by":"Trondheim","kvm":17222,"ma":4,"yr":2015,"s":"nå"},
        {"n":"Trondheim bygg 16 562 kvm","by":"Trondheim","kvm":16562,"ma":4,"yr":2015,"s":"nå"},
        {"n":"Trondheim bygg 15 000 kvm","by":"Trondheim","kvm":15000,"ma":4,"yr":2015,"s":"nå"},
        {"n":"Schweigaardsgate 21","by":"Oslo","kvm":14827,"ma":4,"yr":2013,"s":"nå"},
        {"n":"Schweigaardsgate 23","by":"Oslo","kvm":15093,"ma":4,"yr":2013,"s":"nå"},
        {"n":"Lysaker Torg 35","by":"Lysaker","kvm":14607,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Munkedamsveien 35","by":"Oslo","kvm":14651,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Slemsdalsveien 37","by":"Oslo","kvm":14000,"ma":4,"yr":2016,"s":"nå"},
        {"n":"Trondheim bygg 13 525 kvm","by":"Trondheim","kvm":13525,"ma":4,"yr":2016,"s":"nå"},
        {"n":"Tempeveien 22 (MAX-bygget)","by":"Trondheim","kvm":13400,"ma":4,"yr":2018,"s":"snart"},
        {"n":"Trondheim bygg 12 400 kvm","by":"Trondheim","kvm":12400,"ma":4,"yr":2016,"s":"nå"},
        {"n":"Brynsengveien 10","by":"Oslo","kvm":12300,"ma":4,"yr":2016,"s":"nå"},
        {"n":"Trondheim bygg 11 706 kvm","by":"Trondheim","kvm":11706,"ma":4,"yr":2016,"s":"nå"},
        {"n":"Drammensveien 144","by":"Oslo","kvm":10950,"ma":4,"yr":2018,"s":"snart"},
        {"n":"Stensberggata 26","by":"Oslo","kvm":10600,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Selma Ellefsens vei 8","by":"Oslo","kvm":10000,"ma":3,"yr":2016,"s":"nå"},
        {"n":"Citypassagen","by":"Oslo","kvm":15000,"ma":4,"yr":2020,"s":"snart"},
        {"n":"Holbergssgate 1","by":"Oslo","kvm":11120,"ma":4,"yr":2018,"s":"snart"},
        {"n":"Hansteensgate 2","by":"Oslo","kvm":9000,"ma":4,"yr":2018,"s":"snart"},
        {"n":"Prinsensgate 7-9","by":"Oslo","kvm":9000,"ma":4,"yr":2016,"s":"nå"},
        {"n":"Tjuvholmen allé 11","by":"Oslo","kvm":8200,"ma":4,"yr":2021,"s":"snart"},
        {"n":"Gryta 2B","by":"Trondheim","kvm":8600,"ma":4,"yr":2019,"s":"snart"},
        {"n":"Tjuvholmen allé 16","by":"Oslo","kvm":6544,"ma":4,"yr":2018,"s":"snart"},
        {"n":"Peter Egges plass 2","by":"Trondheim","kvm":7360,"ma":4,"yr":2010,"s":"nå"},
        {"n":"Sluppenveien 12E","by":"Trondheim","kvm":7100,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Munkedamsveien 62 B-D","by":"Oslo","kvm":6570,"ma":4,"yr":2018,"s":"snart"},
        {"n":"Universitetsgata 8","by":"Oslo","kvm":5000,"ma":4,"yr":2018,"s":"snart"},
        {"n":"Brøsetvegen 168","by":"Trondheim","kvm":5593,"ma":4,"yr":2016,"s":"nå"},
        {"n":"Stensberggata 29","by":"Oslo","kvm":3270,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Trondheim bygg 4 250 kvm","by":"Trondheim","kvm":4250,"ma":4,"yr":2016,"s":"nå"},
    ]


def scrape_nordea():
    print("Scraper Nordea Liv Eiendom (hardkodet)...")
    return [
        {"n":"Folke Bernadottes vei 38","by":"Bergen","kvm":26094,"ma":4,"yr":2019,"s":"nå"},
        {"n":"Nykirkebakken 2 / Verksgata 1","by":"Bergen","kvm":19580,"ma":4,"yr":2018,"s":"nå"},
        {"n":"Økernveien 119-121","by":"Oslo","kvm":19325,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Kokstadvegen 23B ★","by":"Bergen","kvm":17000,"ma":4,"yr":2016,"s":"nå"},
        {"n":"Fabrikkveien 36-38","by":"Stavanger","kvm":17962,"ma":3,"yr":2022,"s":"snart"},
        {"n":"Rådhuspassasjen","by":"Oslo","kvm":10125,"ma":5,"yr":None,"s":"ingen"},
        {"n":"Christian Krohgs gate 32","by":"Oslo","kvm":11300,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Dronning Mauds gate 15","by":"Oslo","kvm":9054,"ma":4,"yr":2019,"s":"snart"},
        {"n":"Olav Kyrres gate 22","by":"Bergen","kvm":8965,"ma":4,"yr":2023,"s":"ok"},
        {"n":"Fridtjof Nansens plass 7","by":"Oslo","kvm":6835,"ma":5,"yr":2017,"s":"nå"},
        {"n":"Eikenga 31-33","by":"Oslo","kvm":10851,"ma":3,"yr":2015,"s":"nå"},
        {"n":"Allehelgens gate 4","by":"Bergen","kvm":7558,"ma":4,"yr":2022,"s":"snart"},
        {"n":"Havnespeilet (Sandnes)","by":"Stavanger","kvm":6370,"ma":4,"yr":2018,"s":"nå"},
        {"n":"Cort Adelers gate 33","by":"Oslo","kvm":6313,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Munchs gate 5B","by":"Oslo","kvm":5214,"ma":4,"yr":2024,"s":"ok"},
        {"n":"Kronprinsensgate 17","by":"Oslo","kvm":5096,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Kokstadflaten 4","by":"Bergen","kvm":4397,"ma":4,"yr":2025,"s":"ok"},
        {"n":"Pilestredet 12","by":"Oslo","kvm":4142,"ma":4,"yr":2022,"s":"snart"},
        {"n":"Valhallavegen 6","by":"Oslo","kvm":6092,"ma":2,"yr":2019,"s":"snart"},
        {"n":"Fabrikkveien 41","by":"Stavanger","kvm":4022,"ma":3,"yr":2022,"s":"snart"},
        {"n":"Henrik Ibsens gate 40-42","by":"Oslo","kvm":1782,"ma":4,"yr":None,"s":"ingen"},
    ]


def scrape_are():
    print("Scraper Aspelin Reitan Eiendom (hardkodet)...")
    return [
        {"n":"VIA Vika (Ruseløkkveien 26)","by":"Oslo","kvm":62500,"ma":5,"yr":2021,"s":"snart"},
        {"n":"Rosenholm Campus","by":"Oslo","kvm":43000,"ma":4,"yr":2018,"s":"snart"},
        {"n":"Schweigaards gate 33+28","by":"Oslo","kvm":35000,"ma":4,"yr":2019,"s":"nå"},
        {"n":"Vulkan (kontor/næring)","by":"Oslo","kvm":30000,"ma":4,"yr":2014,"s":"nå"},
        {"n":"Campus Ullevål","by":"Oslo","kvm":25000,"ma":5,"yr":None,"s":"ingen"},
        {"n":"Over Spektrum","by":"Oslo","kvm":22500,"ma":5,"yr":None,"s":"ingen"},
        {"n":"Karvesvingen 7 (Hasle Linie)","by":"Oslo","kvm":21000,"ma":3,"yr":2022,"s":"snart"},
        {"n":"Fr. Nansens vei 17-19","by":"Oslo","kvm":8000,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Essendrops gate 3","by":"Oslo","kvm":6000,"ma":4,"yr":2017,"s":"nå"},
        {"n":"Trelastgården","by":"Oslo","kvm":8000,"ma":4,"yr":2023,"s":"ok"},
        {"n":"Pressens hus","by":"Oslo","kvm":7000,"ma":4,"yr":2019,"s":"snart"},
        {"n":"Vulkan 7-11 (Bellonahuset)","by":"Oslo","kvm":5000,"ma":4,"yr":2010,"s":"nå"},
        {"n":"Hausmannsgate 16","by":"Oslo","kvm":4000,"ma":4,"yr":2012,"s":"nå"},
        {"n":"Schweigaarden (Landbrukskvart.)","by":"Oslo","kvm":9500,"ma":5,"yr":None,"s":"ingen"},
    ]


# ---------------------------------------------------------------------------
# Hjelpefunksjoner
# ---------------------------------------------------------------------------

def status(yr):
    if yr is None:
        return "ingen"
    if yr <= 2018:
        return "nå"
    if yr <= 2022:
        return "snart"
    return "ok"


def _city(raw):
    for city, keywords in [
        ("Bergen", ["Bergen"]),
        ("Trondheim", ["Trondheim"]),
        ("Stavanger", ["Stavanger", "Sandnes"]),
        ("Lysaker", ["Lysaker", "Fornebu"]),
        ("Drammen", ["Drammen"]),
        ("Kristiansand", ["Kristiansand"]),
        ("Tromsø", ["Tromsø"]),
    ]:
        if any(k in raw for k in keywords):
            return city
    return "Oslo"

_city_from_text = _city


def _parse_kvm(text):
    m = re.search(r"([\d][\d\s\xa0 ]{2,8})\s*kvm", text)
    if m:
        try:
            return int(re.sub(r"[\s\xa0 ]", "", m.group(1)))
        except Exception:
            pass
    return None


def _parse_year(text):
    m = re.search(r"(?:bygge[åa]r|ferdigstilt(?:\s+i)?)[:\s]*(\d{4})", text, re.IGNORECASE)
    if m:
        yr = int(m.group(1))
        if 1850 <= yr <= THIS_YEAR:
            return yr
    return None


def to_js(arr):
    lines = []
    for r in arr:
        yr = r["yr"] if r["yr"] else "null"
        n = r["n"].replace("'", "\\'")
        src   = (r.get("src")   or "").replace("'", "\\'")
        kilde = (r.get("kilde") or "").replace("'", "\\'")
        lines.append(f"  {{n:'{n}',by:'{r['by']}',kvm:{r['kvm']},ma:{r['ma']},yr:{yr},s:'{r['s']}',src:'{src}',kilde:'{kilde}'}}")
    return "[\n" + ",\n".join(lines) + "\n]"


def summary(data):
    mfkvm = sum(round(r["kvm"] * r["ma"] / 100) for r in data)
    brutto = sum(round(r["kvm"] * r["ma"] / 100) * RATE for r in data)
    return len(data), round(mfkvm), brutto


# ---------------------------------------------------------------------------
# Bygg HTML
# ---------------------------------------------------------------------------

def build_html(companies):
    total_bygg = sum(len(d) for _, _, d in companies)
    total_brutto = sum(summary(d)[2] for _, _, d in companies)
    total_konservativt = round(total_brutto * 0.8)

    def fmt_m(n):
        return f"NOK {round(n/1e6)} M"

    def tab_html():
        tabs = ['<div class="tab active" onclick="switchTab(\'sammendrag\',this)">Sammendrag</div>']
        for cid, label, data in companies:
            tabs.append(f'<div class="tab" onclick="switchTab(\'{cid}\',this)">{label} <span class="tab-count">{len(data)}</span></div>')
        return "\n    ".join(tabs)

    def summary_cards():
        cards = [f'''          <div class="s-card big">
            <div class="s-label">Konservativt estimat (80%)</div>
            <div class="s-value">{fmt_m(total_konservativt)}</div>
            <div class="s-sub">{len(companies)} selskaper · {total_bygg} bygg</div>
          </div>''']
        for cid, label, data in companies:
            _, _, brutto = summary(data)
            cards.append(f'''          <div class="s-card">
            <div class="s-label">{label}</div>
            <div class="s-value" style="font-size:20px">{fmt_m(brutto)}</div>
            <div class="s-sub">{len(data)} bygg</div>
          </div>''')
        cards.append('''          <div class="s-card">
            <div class="s-label">NOK / møblert kvm</div>
            <div class="s-value" style="font-size:20px">7 353</div>
            <div class="s-sub">Kokstadvegen 23B ★ · referanserate</div>
          </div>''')
        return "\n".join(cards)

    def summary_table_rows():
        rows = []
        for cid, label, data in companies:
            cnt, mfkvm, brutto = summary(data)
            total_kvm = sum(r["kvm"] for r in data)
            rows.append(
                f'            <tr><td>{label}</td><td class="r">{cnt}</td>'
                f'<td class="r">~{total_kvm:,}</td><td class="r">~{mfkvm:,}</td>'
                f'<td class="r">{fmt_m(brutto)}</td><td class="r">{fmt_m(round(brutto*0.8))}</td></tr>'
            )
        total_kvm_all = sum(r["kvm"] for _, _, d in companies for r in d)
        total_mf_all = sum(summary(d)[1] for _, _, d in companies)
        rows.append(
            f'            <tr><td>TOTAL</td><td class="r">{total_bygg}</td>'
            f'<td class="r">~{total_kvm_all:,}</td><td class="r">~{total_mf_all:,}</td>'
            f'<td class="r">{fmt_m(total_brutto)}</td><td class="r">{fmt_m(total_konservativt)}</td></tr>'
        )
        return "\n".join(rows)

    def panes():
        html = []
        for cid, label, data in companies:
            city_filters = sorted(set(r["by"] for r in data))
            city_btns = ""
            if len(city_filters) > 1:
                for city in city_filters:
                    city_btns += f'\n        <button class="filter-btn" onclick="filter(\'{cid}\',\'{city.lower()}\',this)">{city}</button>'
            _, _, brutto = summary(data)
            html.append(f'''
    <div class="pane" id="pane-{cid}">
      <div class="toolbar">
        <button class="filter-btn on" onclick="filter('{cid}','all',this)">Alle</button>
        <button class="filter-btn" onclick="filter('{cid}','nå',this)">Nå</button>
        <button class="filter-btn" onclick="filter('{cid}','snart',this)">Snart</button>
        <button class="filter-btn" onclick="filter('{cid}','ok',this)">OK</button>
        <button class="filter-btn" onclick="filter('{cid}','ingen',this)">Ingen ennå</button>{city_btns}
        <div class="toolbar-sum">Synlig: <b id="sum-{cid}">{fmt_m(brutto)}</b></div>
      </div>
      <div class="tbl-wrap"><table>
        <colgroup><col style="width:36px"><col style="width:260px"><col style="width:90px"><col style="width:55px"><col style="width:85px"><col style="width:115px"><col style="width:90px"><col style="width:75px"><col style="width:95px"><col style="width:90px"></colgroup>
        <thead><tr><th class="rn">#</th><th>Bygg</th><th class="r">Brutto kvm</th><th class="c">MA%</th><th class="r">Møblert kvm</th><th class="r">Møbelverdi</th><th class="c">Møbelalder</th><th class="c">Byggeår</th><th class="c">Status</th><th>By</th></tr></thead>
        <tbody id="tbody-{cid}"></tbody>
      </table></div>
    </div>''')
        return "\n".join(html)

    def js_data():
        lines = []
        for cid, label, data in companies:
            var = cid.upper().replace("-", "_")
            lines.append(f"const {var} = {to_js(data)};")
        return "\n\n".join(lines)

    def render_calls():
        return "\n".join(
            f"renderTable('{cid}', {cid.upper().replace('-','_')});"
            for cid, _, _ in companies
        )

    today = date.today().strftime("%-d. %B %Y")

    return f"""<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Minus — Møbelverdi</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --bg: #f2f1ed; --sheet: #ffffff; --ink: #1a1a1a; --ink-2: #555; --ink-3: #999;
  --border: #d8d5cc; --accent: #1c3d5a;
  --now-bg: #fff0ee; --now: #b83020; --soon-bg: #fffbec; --soon: #8a6000;
  --ok-bg: #edfaf3; --ok: #1a6e3c; --ingen-bg: #eef5ff; --ingen: #1a4a8a;
  --tab-h: 32px; --header-h: 44px; --row-h: 28px;
}}
body {{ font-family: 'IBM Plex Sans', sans-serif; background: var(--bg); color: var(--ink); font-size: 12.5px; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }}
.topbar {{ height: var(--header-h); background: var(--accent); display: flex; align-items: center; padding: 0 18px; gap: 24px; flex-shrink: 0; }}
.topbar-logo {{ font-family: 'IBM Plex Mono', monospace; font-size: 12px; font-weight: 500; color: white; letter-spacing: 3px; text-transform: uppercase; }}
.topbar-updated {{ font-size: 10px; color: rgba(255,255,255,0.35); margin-left: 8px; }}
.topbar-kpis {{ margin-left: auto; display: flex; gap: 32px; }}
.kpi {{ text-align: right; }}
.kpi-l {{ font-size: 9px; letter-spacing: 1px; text-transform: uppercase; color: rgba(255,255,255,0.4); }}
.kpi-v {{ font-family: 'IBM Plex Mono', monospace; font-size: 14px; color: white; }}
.kpi-v.gold {{ color: #dbb84a; }}
.tabbar {{ display: flex; align-items: flex-end; padding: 8px 14px 0; background: var(--bg); gap: 2px; flex-shrink: 0; overflow-x: auto; }}
.tab {{ height: var(--tab-h); padding: 0 16px; border: 1px solid var(--border); border-bottom: none; background: #e5e2db; color: var(--ink-2); font-size: 12px; cursor: pointer; border-radius: 3px 3px 0 0; display: flex; align-items: center; gap: 8px; user-select: none; white-space: nowrap; transition: background 0.1s; }}
.tab:hover {{ background: #eeebe4; }}
.tab.active {{ background: var(--sheet); color: var(--ink); font-weight: 500; border-bottom: 1px solid var(--sheet); z-index: 2; position: relative; }}
.tab-count {{ font-family: 'IBM Plex Mono', monospace; font-size: 10px; color: var(--ink-3); background: rgba(0,0,0,0.07); padding: 0 5px; border-radius: 2px; }}
.tab.active .tab-count {{ background: #eee; }}
.sheet-content {{ flex: 1; overflow: hidden; background: var(--sheet); border: 1px solid var(--border); margin: 0 14px 14px; display: flex; flex-direction: column; }}
.pane {{ display: none; flex-direction: column; height: 100%; overflow: hidden; }}
.pane.active {{ display: flex; }}
.toolbar {{ display: flex; align-items: center; padding: 6px 12px; gap: 6px; border-bottom: 1px solid var(--border); background: #fafaf7; flex-shrink: 0; flex-wrap: wrap; }}
.filter-btn {{ padding: 2px 9px; border: 1px solid var(--border); background: white; border-radius: 2px; font-size: 11px; cursor: pointer; color: var(--ink-2); }}
.filter-btn:hover {{ background: var(--bg); }}
.filter-btn.on {{ background: var(--accent); color: white; border-color: var(--accent); }}
.toolbar-sum {{ margin-left: auto; font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--ink-2); }}
.toolbar-sum b {{ color: var(--accent); }}
.tbl-wrap {{ flex: 1; overflow: auto; }}
table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
thead th {{ position: sticky; top: 0; z-index: 1; background: #eeece6; border: 1px solid var(--border); padding: 0 10px; height: 26px; font-size: 10.5px; font-weight: 600; color: var(--ink-2); text-align: left; white-space: nowrap; }}
thead th.r {{ text-align: right; }} thead th.c {{ text-align: center; }}
thead th.rn {{ width:36px; background:#e8e5de; text-align:center; color:var(--ink-3); }}
tbody td {{ border: 1px solid #ebe8e0; padding: 0 10px; height: var(--row-h); vertical-align: middle; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
tbody td.rn {{ width:36px; background:#f5f3ef; text-align:center; font-family:'IBM Plex Mono',monospace; font-size:10px; color:var(--ink-3); border-right:1px solid var(--border); }}
tbody td.r {{ text-align: right; font-family: 'IBM Plex Mono', monospace; }}
tbody td.c {{ text-align: center; }}
tbody tr:hover td {{ filter: brightness(0.97); }}
tr.row-nå td {{ background: var(--now-bg); }} tr.row-snart td {{ background: var(--soon-bg); }}
tr.row-ok td {{ background: var(--ok-bg); }} tr.row-ingen td {{ background: var(--ingen-bg); }}
tr.row-total td {{ background: #eeece6 !important; font-weight: 600; border-top: 2px solid #bbb; }}
tr.hidden {{ display: none; }}
.badge {{ display:inline-block; font-size:9.5px; font-weight:600; padding:1px 6px; border-radius:2px; }}
.b-nå {{ background:#fdd; color:var(--now); }} .b-snart {{ background:#fef3cc; color:var(--soon); }}
.b-ok {{ background:#d4edda; color:var(--ok); }} .b-ingen {{ background:#d6eaf8; color:var(--ingen); }}
.sum-wrap {{ padding: 20px; flex: 1; overflow: auto; }}
.sum-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:1px; background:var(--border); border:1px solid var(--border); margin-bottom:20px; }}
.s-card {{ background:white; padding:18px 20px; }}
.s-card.big {{ grid-column:span 2; background:var(--accent); }}
.s-label {{ font-size:9px; letter-spacing:1.5px; text-transform:uppercase; color:var(--ink-3); margin-bottom:6px; }}
.s-card.big .s-label {{ color:rgba(255,255,255,0.45); }}
.s-value {{ font-family:'IBM Plex Mono',monospace; font-size:26px; font-weight:500; color:var(--accent); }}
.s-card.big .s-value {{ color:#dbb84a; font-size:30px; }}
.s-sub {{ font-size:11px; color:var(--ink-3); margin-top:3px; }}
.s-card.big .s-sub {{ color:rgba(255,255,255,0.35); }}
.sum-table {{ width:100%; border-collapse:collapse; border:1px solid var(--border); }}
.sum-table th {{ background:#eeece6; border:1px solid var(--border); padding:6px 12px; font-size:10.5px; color:var(--ink-2); text-align:left; }}
.sum-table th.r {{ text-align:right; }}
.sum-table td {{ border:1px solid var(--border); padding:7px 12px; font-size:12px; }}
.sum-table td.r {{ text-align:right; font-family:'IBM Plex Mono',monospace; }}
.sum-table tr:last-child td {{ font-weight:600; background:#eeece6; border-top:2px solid #bbb; }}
</style>
</head>
<body>
<div class="topbar">
  <div class="topbar-logo">Minus<span class="topbar-updated">oppdatert {today}</span></div>
  <div class="topbar-kpis">
    <div class="kpi"><div class="kpi-l">Brutto estimat</div><div class="kpi-v gold">{fmt_m(total_brutto)}</div></div>
    <div class="kpi"><div class="kpi-l">Konservativt (80%)</div><div class="kpi-v">{fmt_m(total_konservativt)}</div></div>
    <div class="kpi"><div class="kpi-l">Selskaper</div><div class="kpi-v">{len(companies)}</div></div>
    <div class="kpi"><div class="kpi-l">Bygg</div><div class="kpi-v">{total_bygg}</div></div>
  </div>
</div>
<div style="display:flex;flex-direction:column;flex:1;overflow:hidden">
  <div class="tabbar">
    <div class="tab active" onclick="switchTab('sammendrag',this)">Sammendrag</div>
    {tab_html()}
  </div>
  <div class="sheet-content">
    <div class="pane active" id="pane-sammendrag">
      <div class="sum-wrap">
        <div class="sum-grid">
{summary_cards()}
        </div>
        <table class="sum-table">
          <thead><tr>
            <th>Selskap</th><th class="r">Bygg</th><th class="r">Brutto kvm</th><th class="r">Møblert kvm</th><th class="r">Brutto møbelverdi</th><th class="r">Konservativt (80%)</th>
          </tr></thead>
          <tbody>
{summary_table_rows()}
          </tbody>
        </table>
      </div>
    </div>
{panes()}
  </div>
</div>
<script>
const RATE = {RATE};
const fmtN = n => n >= 1e6 ? 'NOK '+(n/1e6).toFixed(1)+' M' : 'NOK '+(n/1e3).toFixed(0)+' k';
const fmtK = n => n.toLocaleString('no');
const badge = {{
  nå:'<span class="badge b-nå">Nå</span>',
  snart:'<span class="badge b-snart">Snart</span>',
  ok:'<span class="badge b-ok">OK</span>',
  ingen:'<span class="badge b-ingen">Ingen ennå</span>',
}};

{js_data()}

function renderTable(id, data) {{
  data.sort((a,b) => (b.kvm*b.ma/100 - a.kvm*a.ma/100));
  const tbody = document.getElementById('tbody-'+id);
  let html = '', total = 0;
  data.forEach((r, i) => {{
    const maKvm = Math.round(r.kvm * r.ma / 100);
    const v = Math.round(maKvm * RATE);
    total += v;
    const age = r.yr ? {THIS_YEAR} - r.yr : null;
    const alderTall = (age !== null && age <= 10) ? age + ' år' : '—';
    const alder = (r.kilde && age !== null && age <= 10)
      ? `${{alderTall}} <a href="${{r.src}}" target="_blank" rel="noopener" title="Kilde: ${{r.kilde}}" style="font-size:9.5px;color:var(--accent);text-decoration:none;opacity:0.7">via ${{r.kilde}} ↗</a>`
      : alderTall;
    const yrCell = r.yr
      ? `<span style="font-family:'IBM Plex Mono',monospace">${{r.yr}}</span>`
      : '—';
    html += `<tr class="row-${{r.s}}" data-s="${{r.s}}" data-by="${{r.by.toLowerCase()}}">
      <td class="rn">${{i+1}}</td><td title="${{r.n}}">${{r.n}}</td>
      <td class="r">${{fmtK(r.kvm)}}</td><td class="c">${{r.ma}}%</td>
      <td class="r">${{fmtK(maKvm)}}</td><td class="r">${{fmtN(v)}}</td>
      <td class="c" style="color:#999;font-size:11px;font-style:italic">${{alder}}</td>
      <td class="c">${{yrCell}}</td>
      <td class="c">${{badge[r.s]||''}}</td><td>${{r.by}}</td></tr>`;
  }});
  html += `<tr class="row-total"><td></td><td><b>TOTAL</b></td><td class="r"></td><td></td><td class="r"></td><td class="r"><b>${{fmtN(total)}}</b></td><td colspan="4"></td></tr>`;
  tbody.innerHTML = html;
}}

{render_calls()}

function switchTab(id, el) {{
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('pane-'+id).classList.add('active');
  el.classList.add('active');
}}

function filter(id, f, btn) {{
  btn.closest('.toolbar').querySelectorAll('.filter-btn').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  const rows = document.querySelectorAll(`#tbody-${{id}} tr:not(.row-total)`);
  let total = 0;
  rows.forEach(r => {{
    const show = f === 'all' || r.dataset.s === f || r.dataset.by.includes(f);
    r.classList.toggle('hidden', !show);
    if (show) {{
      const cell = r.cells[5]?.textContent || '';
      const m = cell.match(/([\d.,]+)\s*M/);
      const k = cell.match(/([\d.,]+)\s*k/);
      if (m) total += parseFloat(m[1].replace(',','.')) * 1e6;
      else if (k) total += parseFloat(k[1].replace(',','.')) * 1e3;
    }}
  }});
  document.getElementById('sum-'+id).textContent = fmtN(total);
}}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from signals import hent_signaler, appliser_signaler

    # 1. Kjente selskaper med tilpassede scrapere
    known = [
        ("nordea", "Nordea Liv Eiendom", scrape_nordea()),
        ("klp",    "KLP Eiendom",        scrape_klp()),
        ("are",    "Aspelin Reitan",     scrape_are()),
        ("entra",  "Entra",              scrape_entra()),
    ]

    # 2. Automatisk oppdagelse via Brreg — kun selskaper over MIN_KVM-proxy
    print(f"\nOppdager nye selskaper (terskel: omsetning ≥ {MIN_OMSETNING_NOK/1e6:.0f} MNOK)...")
    kjente_navn = {label.lower() for _, label, _ in known}
    kandidater = brreg_finn_kandidater()

    auto = []
    for k in kandidater:
        navn = k["navn"]
        if any(kj in navn.lower() for kj in kjente_navn):
            continue  # Allerede dekket

        hjemmeside = k.get("hjemmeside") or finn_hjemmeside(k["orgnr"], navn)
        if not hjemmeside:
            print(f"  {navn}: ingen hjemmeside funnet, hopper over")
            continue

        print(f"  Prøver {navn} ({hjemmeside[:50]})...")
        bygg = scrape_nettsted(navn, hjemmeside)
        if bygg:
            total_kvm = sum(b["kvm"] for b in bygg)
            if total_kvm >= MIN_KVM:
                cid = re.sub(r"[^a-z0-9]", "-", navn.lower())[:20].strip("-")
                auto.append((cid, navn, bygg))
                print(f"    ✓ {len(bygg)} bygg | {total_kvm:,} kvm")
            else:
                print(f"    Under {MIN_KVM:,} kvm terskel ({total_kvm:,} kvm) — hopper over")
        else:
            print(f"    Ingen scrapbar data")
        time.sleep(0.5)

    companies = known + auto
    print(f"\nTotalt: {len(companies)} selskaper")

    # 3. Hent oppussings-/innredningssignaler og oppdater møbelstatus
    print("\nHenter signaler fra arkitekt-/interiørfirmaer...")
    signaler = hent_signaler()
    total_oppdatert = 0
    companies_med_signaler = []
    for cid, label, bygg in companies:
        oppdatert_bygg, n = appliser_signaler(bygg, signaler)
        total_oppdatert += n
        companies_med_signaler.append((cid, label, oppdatert_bygg))
    companies = companies_med_signaler
    print(f"  {total_oppdatert} bygg fikk oppdatert møbelstatus fra signaler")

    # 4. Bygg og lagre HTML
    html = build_html(companies)
    out = Path(__file__).parent.parent / "index.html"
    out.write_text(html, encoding="utf-8")

    total = sum(len(d) for _, _, d in companies)
    brutto = sum(summary(d)[2] for _, _, d in companies)
    print(f"Ferdig → {out}")
    print(f"Totalt: {total} bygg | NOK {brutto/1e6:.0f} M brutto | NOK {brutto*0.8/1e6:.0f} M konservativt")
