"""
Henter oppussings- og innredningssignaler fra norske arkitekt- og
interiørfirmaers prosjektportføljer. Matcher mot kjente bygg i porteføljen
og returnerer signaler på nylig møbelskifte.

Returformat per signal:
  {
    "adresse":  "Lakkegata 53",
    "selskap":  "Entra",
    "yr_signal": 2023,          # År prosjektet ble ferdigstilt/publisert
    "kilde":    "Metropolis",
    "url":      "https://..."
  }
"""

import requests
from bs4 import BeautifulSoup
import re
import time
import warnings
from datetime import date

warnings.filterwarnings("ignore")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
}
THIS_YEAR = date.today().year

# ---------------------------------------------------------------------------
# Søkeord vi matcher mot — adresser og selskapsnavn fra porteføljen
# ---------------------------------------------------------------------------

ADRESSE_TREFF = [
    "Lakkegata 53", "Verkstedveien 1", "Verkstedveien 3",
    "Biskop Gunnerus", "Hagegata 22", "Grensesvingen",
    "Pilestredet 33", "Pilestredet 40", "Pilestredet 75",
    "Akersgata 55", "Akersgata 64", "Akersgata 34",
    "Schweigaard", "Fyrstikkalléen", "Fyrstikkalleen",
    "Universitetsgata 2", "Drammensveien 134", "Drammensveien 288",
    "Lars Hilles gate", "Nygårdsgaten 95", "Nonnesetergaten",
    "Trondheimsveien 2", "Stortorvet 7",
    "Ruseløkkveien 26", "Rosenholm",
    "Folke Bernadottes", "Kokstadvegen", "Nykirkebakken",
    "Fabrikkveien 36", "Eikenga",
    "Brynsengfaret", "Stenersgata",
]

SELSKAP_TREFF = [
    "Entra", "KLP Eiendom", "KLP", "Nordea Liv",
    "Aspelin Reitan", "Norwegian Property", "NPRO",
    "Storebrand Eiendom", "DNB Eiendom",
]

# ---------------------------------------------------------------------------
# Arkitekt-/interiørfirmaer med scrapbare prosjektportføljer
# ---------------------------------------------------------------------------

FIRMAER = [
    {
        "navn": "Metropolis",
        "liste_url": "https://www.metropolis.no/prosjekter/",
        "proj_pattern": r"metropolis\.no/prosjekter/[^/\"']+/$",
        "felt": {
            "adresse":   r"Adresse[:\s]*([^\n|]{5,80})",
            "klient":    r"Oppdragsgiver[:\s]*([^\n|]{3,80})",
            "yr":        r"(?:Ferdigstilt|År)[:\s]*(\d{4})",
            "kvm":       r"Kvm[:\s]*([\d\s]+)",
        },
    },
    {
        "navn": "Mad arkitekter",
        "liste_url": "https://www.mad.no/prosjekter/",
        "proj_pattern": r"mad\.no/prosjekter/[^/\"']+/?$",
        "felt": {
            "adresse":   r"Adresse[:\s]*([^\n|]{5,80})",
            "klient":    r"(?:Oppdragsgiver|Klient|Byggherre)[:\s]*([^\n|]{3,80})",
            "yr":        r"(?:Ferdigstilt|Årstall|År)[:\s]*(\d{4})",
        },
    },
    {
        "navn": "Norconsult",
        "liste_url": "https://www.norconsult.no/prosjekter/",
        "proj_pattern": r"norconsult\.no/prosjekter/[^/\"']+/?$",
        "felt": {
            "adresse":   r"Adresse[:\s]*([^\n|]{5,80})",
            "klient":    r"(?:Oppdragsgiver|Kunde|Klient)[:\s]*([^\n|]{3,80})",
            "yr":        r"(?:Ferdigstilt|År|Årstall)[:\s]*(\d{4})",
        },
    },
    {
        "navn": "Snøhetta",
        "liste_url": "https://snohetta.com/projects/",
        "proj_pattern": r"snohetta\.com/projects/[^/\"']+/?$",
        "felt": {
            "klient":    r"(?:Client|Klient)[:\s]*([^\n|]{3,80})",
            "yr":        r"(?:Year|Completed|Ferdigstilt)[:\s]*(\d{4})",
        },
    },
    {
        "navn": "Asplan Viak",
        "liste_url": "https://www.asplanviak.no/prosjekter/",
        "proj_pattern": r"asplanviak\.no/prosjekter/[^/\"']+/?$",
        "felt": {
            "adresse":   r"Adresse[:\s]*([^\n|]{5,80})",
            "klient":    r"(?:Oppdragsgiver|Klient)[:\s]*([^\n|]{3,80})",
            "yr":        r"(?:Ferdigstilt|År)[:\s]*(\d{4})",
        },
    },
]

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _match_bygg(text):
    """Returnerer første adresse- eller selskapsstreng som matcher porteføljen."""
    for adr in ADRESSE_TREFF:
        if adr.lower() in text.lower():
            return adr, None
    for sel in SELSKAP_TREFF:
        if re.search(r'\b' + re.escape(sel) + r'\b', text, re.IGNORECASE):
            return None, sel
    return None, None


def _extract_year(text, felt):
    """Prøver strukturert felt, faller tilbake på siste 20xx-år i teksten."""
    if "yr" in felt:
        m = re.search(felt["yr"], text, re.IGNORECASE)
        if m:
            yr = int(m.group(1))
            if 2000 <= yr <= THIS_YEAR:
                return yr

    # Fallback: siste year-tag eller publiseringsdato i HTML
    years = [int(y) for y in re.findall(r'\b(20[12]\d)\b', text) if 2000 <= int(y) <= THIS_YEAR]
    if years:
        return max(years)
    return None


def _scrape_firma(firma):
    """Scraper ett firma og returnerer liste med signaler."""
    signaler = []
    try:
        r = requests.get(firma["liste_url"], headers=HEADERS, timeout=12)
        if r.status_code != 200:
            return signaler

        soup = BeautifulSoup(r.text, "lxml")
        proj_links = list(set([
            a["href"] if a["href"].startswith("http") else "https://" + a["href"].lstrip("/")
            for a in soup.find_all("a", href=True)
            if re.search(firma["proj_pattern"], a["href"])
        ]))

        for url in proj_links[:60]:
            try:
                pr = requests.get(url, headers=HEADERS, timeout=10)
                if pr.status_code != 200:
                    continue
                text = BeautifulSoup(pr.text, "lxml").get_text(" ", strip=True)

                adr, sel = _match_bygg(text)
                if not adr and not sel:
                    time.sleep(0.1)
                    continue

                yr = _extract_year(text, firma["felt"])

                # Hent klient fra strukturert felt
                klient = None
                if "klient" in firma["felt"]:
                    km = re.search(firma["felt"]["klient"], text, re.IGNORECASE)
                    if km:
                        klient = km.group(1).strip()[:80]

                signaler.append({
                    "adresse":   adr,
                    "selskap":   sel or klient,
                    "yr_signal": yr,
                    "kilde":     firma["navn"],
                    "url":       url,
                })
                time.sleep(0.15)
            except Exception:
                pass

    except Exception:
        pass

    return signaler


# ---------------------------------------------------------------------------
# Hoved-funksjon
# ---------------------------------------------------------------------------

def hent_signaler(verbose=True):
    """
    Kjører alle firmaer og returnerer liste med oppussings-/innredningssignaler.
    Filtrerer vekk signaler uten årstall eller eldre enn 10 år.
    """
    alle = []
    for firma in FIRMAER:
        if verbose:
            print(f"  Signaler: {firma['navn']}...", end=" ", flush=True)
        s = _scrape_firma(firma)
        if verbose:
            print(f"{len(s)} treff")
        alle.extend(s)

    # Filtrer: kun signaler med år og ikke for gamle
    cutoff = THIS_YEAR - 10
    filtrert = [s for s in alle if s["yr_signal"] and s["yr_signal"] >= cutoff]

    if verbose:
        print(f"  Totalt: {len(alle)} råsignaler → {len(filtrert)} etter filtrering")

    return filtrert


def appliser_signaler(bygg_liste, signaler):
    """
    Tar en liste med bygg-dicts og en liste med signaler.
    Oppdaterer yr og s for bygg der vi har et nyere signal.
    Returnerer (oppdatert_liste, antall_oppdatert).
    """
    oppdatert = 0
    for bygg in bygg_liste:
        navn = bygg.get("n", "").lower()
        by   = bygg.get("by", "").lower()

        for sig in signaler:
            adr = (sig.get("adresse") or "").lower()
            sel = (sig.get("selskap") or "").lower()
            yr  = sig.get("yr_signal")

            # Match: adresse finnes i byggnavn, eller selskap matcher by + byggnavn
            adresse_match = adr and any(a.lower() in navn for a in adr.split(",")[:1])
            selskap_match = sel and sel in navn

            if (adresse_match or selskap_match) and yr:
                if not bygg.get("yr") or yr > bygg["yr"]:
                    bygg["yr"] = yr
                    bygg["s"]  = _ny_status(yr)
                    oppdatert += 1

    return bygg_liste, oppdatert


def _ny_status(yr):
    if yr >= THIS_YEAR - 3:
        return "ok"
    if yr >= THIS_YEAR - 7:
        return "snart"
    return "nå"


# ---------------------------------------------------------------------------
# Kjør frittstående for test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Henter oppussings-/innredningssignaler...\n")
    signaler = hent_signaler()
    print(f"\n{len(signaler)} signaler totalt:\n")
    for s in signaler:
        print(f"  [{s['yr_signal']}] {s['adresse'] or s['selskap']} — {s['kilde']}")
        print(f"    {s['url']}")
