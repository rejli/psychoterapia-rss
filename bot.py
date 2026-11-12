#!/usr/bin/env python3
"""
Bot RSS: nowe badania z psychoterapii.

Co robi przy każdym uruchomieniu:
1. Pobiera z PubMed artykuły dodane w ostatnich dniach.
2. Wysyła abstrakt każdego z nich do modelu AI (przez OpenRouter),
   który ocenia trafność i pisze streszczenie po polsku.
3. Dopisuje najlepsze artykuły do pliku feed.xml (kanał RSS).

Nie wymaga instalowania żadnych dodatkowych bibliotek.
"""

import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

# ----------------------------------------------------------------------
# USTAWIENIA - to możesz zmieniać
# ----------------------------------------------------------------------

# Model AI na OpenRouter (identyfikator ze strony openrouter.ai/models).
MODEL = "mistralai/mistral-small-3.2-24b-instruct"

# Czego szukać w PubMed. Dopisz lub usuń hasła według potrzeb.
TEMATY = [
    '"psychotherapy"[MeSH Terms]',
    'psychotherap*[Title/Abstract]',
    '"cognitive behavioral therapy"[Title/Abstract]',
    '"cognitive behavioural therapy"[Title/Abstract]',
    '"schema therapy"[Title/Abstract]',
    '"psychodynamic"[Title/Abstract]',
    '"EMDR"[Title/Abstract]',
    '"dialectical behavior therapy"[Title/Abstract]',
    '"acceptance and commitment therapy"[Title/Abstract]',
    '"mentalization"[Title/Abstract]',
    '"therapeutic alliance"[Title/Abstract]',
    '"psychological intervention"[Title/Abstract]',
    '"psychological treatment"[Title/Abstract]',
]

# Opis Twoich zainteresowań - model ocenia trafność względem tego opisu.
ZAINTERESOWANIA = (
    "Psychoterapia i psychologia kliniczna: skuteczność różnych nurtów terapii, "
    "badania nad procesem terapii i relacją terapeutyczną, czynniki leczące, "
    "nowe interwencje psychologiczne, terapia zaburzeń psychicznych u dorosłych, "
    "dzieci i młodzieży."
)

DNI_WSTECZ = 3          # z ilu ostatnich dni brać artykuły
MAKS_KANDYDATOW = 40    # ile artykułów najwyżej oceniać modelem w jednym uruchomieniu
MIN_OCENA = 6           # minimalna trafność (1-10), żeby artykuł trafił do kanału
MAKS_NOWYCH = 12        # ile najwyżej nowych wpisów na jedno uruchomienie
MAKS_W_KANALE = 200     # ile wpisów trzymać w kanale

TYTUL_KANALU = "Psychoterapia – nowe badania"
OPIS_KANALU = "Automatyczny przegląd nowych badań z psychoterapii i psychologii klinicznej, streszczany przez AI."

# ----------------------------------------------------------------------
# Dalej jest już sam mechanizm - nie trzeba nic zmieniać
# ----------------------------------------------------------------------

PLIK_STANU = "state.json"
PLIK_KANALU = "feed.xml"
PUBMED = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
UA = "psychoterapia-rss-bot/1.0"

# Kolejność pierwszeństwa: im wcześniej na liście, tym chętniej oceniamy.
PRIORYTET_TYPOW = [
    "Meta-Analysis",
    "Systematic Review",
    "Randomized Controlled Trial",
    "Clinical Trial",
    "Review",
]
POMIJANE_TYPY = {
    "Comment", "Editorial", "Letter", "Published Erratum", "News",
    "Retraction of Publication", "Retracted Publication", "Biography",
}
DOZWOLONE_ETYKIETY = [
    "Metaanaliza", "Przegląd systematyczny", "Badanie z randomizacją (RCT)",
    "Badanie kliniczne", "Badanie obserwacyjne", "Badanie jakościowe",
    "Przegląd", "Preprint", "Protokół badania", "Inne",
]


def http_get(url, proby=3):
    for i in range(proby):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if i == proby - 1:
                raise
            print(f"  błąd pobierania ({e}), ponawiam...")
            time.sleep(5 * (i + 1))


def http_post_json(url, dane, naglowki, proby=3):
    body = json.dumps(dane).encode("utf-8")
    for i in range(proby):
        try:
            req = urllib.request.Request(url, data=body, headers=naglowki, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            tresc = e.read().decode("utf-8", "replace")[:500]
            # 401/402/404 = zły klucz, brak środków, zły model: ponawianie nic nie da
            if e.code in (400, 401, 402, 403, 404) or i == proby - 1:
                raise RuntimeError(f"OpenRouter HTTP {e.code}: {tresc}") from e
            time.sleep(10 * (i + 1))
        except (urllib.error.URLError, TimeoutError) as e:
            if i == proby - 1:
                raise RuntimeError(f"OpenRouter: {e}") from e
            time.sleep(10 * (i + 1))


# ---------------------------- PubMed ----------------------------------

def szukaj_pubmed():
    zapytanie = "(" + " OR ".join(TEMATY) + ") AND hasabstract AND english[Language]"
    parametry = urllib.parse.urlencode({
        "db": "pubmed", "term": zapytanie, "reldate": DNI_WSTECZ,
        "datetype": "edat", "retmax": 300, "retmode": "json", "sort": "date",
    })
    wynik = json.loads(http_get(PUBMED + "esearch.fcgi?" + parametry))
    return wynik.get("esearchresult", {}).get("idlist", [])


def tekst(el):
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def pobierz_artykuly(pmidy):
    artykuly = []
    for start in range(0, len(pmidy), 100):
        paczka = pmidy[start:start + 100]
        parametry = urllib.parse.urlencode({
            "db": "pubmed", "id": ",".join(paczka), "retmode": "xml",
        })
        root = ET.fromstring(http_get(PUBMED + "efetch.fcgi?" + parametry))
        for art in root.findall(".//PubmedArticle"):
            a = parsuj_artykul(art)
            if a:
                artykuly.append(a)
        time.sleep(0.5)  # PubMed prosi, by nie odpytywać zbyt szybko
    return artykuly


def parsuj_artykul(art):
    pmid = tekst(art.find(".//MedlineCitation/PMID"))
    tytul = tekst(art.find(".//Article/ArticleTitle"))
    czesci = []
    for ab in art.findall(".//Article/Abstract/AbstractText"):
        etykieta = ab.get("Label")
        t = tekst(ab)
        if t:
            czesci.append(f"{etykieta}: {t}" if etykieta else t)
    abstrakt = "\n".join(czesci)
    if not (pmid and tytul and abstrakt):
        return None
    typy = [tekst(t) for t in art.findall(".//PublicationTypeList/PublicationType")]
    doi = ""
    for aid in art.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "doi":
            doi = tekst(aid)
    autorzy = [tekst(a.find("LastName")) for a in art.findall(".//AuthorList/Author")
               if a.find("LastName") is not None]
    if len(autorzy) > 2:
        autor = f"{autorzy[0]} i in."
    else:
        autor = " i ".join(autorzy)
    return {
        "pmid": pmid, "tytul": tytul, "abstrakt": abstrakt, "typy": typy,
        "czasopismo": tekst(art.find(".//Article/Journal/Title")),
        "doi": doi, "autor": autor,
    }


def ranga(artykul):
    for i, typ in enumerate(PRIORYTET_TYPOW):
        if typ in artykul["typy"]:
            return i
    return len(PRIORYTET_TYPOW)


# ---------------------------- Model AI --------------------------------

INSTRUKCJA = """Jesteś redaktorem naukowym, który przygotowuje po polsku przegląd badań dla psychoterapeuty.
Dostaniesz tytuł, typ publikacji i abstrakt artykułu naukowego.

Zasady:
- Opieraj się WYŁĄCZNIE na abstrakcie. Nie dodawaj liczb, wyników ani wniosków, których w nim nie ma.
- Jeśli jakiejś informacji nie ma w abstrakcie, napisz "nie podano w abstrakcie".
- Pisz zwięźle, konkretnie, zrozumiałym językiem. Liczby (wielkość próby, wielkości efektu, przedziały ufności) przepisuj dokładnie.
- Nie wyolbrzymiaj: wyniki małych, wstępnych lub obserwacyjnych badań opisuj ostrożnie.

Zainteresowania czytelnika: {zainteresowania}

Zwróć wyłącznie obiekt JSON z polami:
- "trafnosc": liczba całkowita 1-10. Jak bardzo artykuł jest wart uwagi czytelnika. 8-10: metaanalizy, duże badania z randomizacją, ważne badania procesu terapii. 5-7: solidne badania o węższym znaczeniu. 1-4: protokoły bez wyników, badania czysto farmakologiczne lub biologiczne, tematy luźno związane z psychoterapią, bardzo małe badania pilotażowe.
- "typ_badania": dokładnie jedna z wartości: {etykiety}
- "tytul_pl": tytuł artykułu przetłumaczony na polski
- "streszczenie": 2-3 zdania: co badano i dlaczego
- "metoda": 1-2 zdania: rodzaj badania, uczestnicy, wielkość próby, porównywane warunki
- "wynik": 1-3 zdania: najważniejsze wyniki z liczbami, jeśli są w abstrakcie
- "ograniczenia": 1-2 zdania: ograniczenia podane w abstrakcie lub wynikające wprost z opisanej metody
- "wniosek_dla_praktyki": 1-2 zdania: co z tego wynika dla pracy psychoterapeuty"""


def wyciagnij_json(odpowiedz):
    odpowiedz = odpowiedz.strip()
    poczatek, koniec = odpowiedz.find("{"), odpowiedz.rfind("}")
    if poczatek == -1 or koniec == -1:
        raise ValueError("brak JSON w odpowiedzi modelu")
    return json.loads(odpowiedz[poczatek:koniec + 1])


def ocen_i_streszcz(artykul, klucz):
    system = INSTRUKCJA.format(
        zainteresowania=ZAINTERESOWANIA, etykiety=", ".join(DOZWOLONE_ETYKIETY))
    uzytkownik = (
        f"Tytuł: {artykul['tytul']}\n"
        f"Czasopismo: {artykul['czasopismo']}\n"
        f"Typ publikacji wg PubMed: {', '.join(artykul['typy']) or 'brak'}\n\n"
        f"Abstrakt:\n{artykul['abstrakt'][:6000]}"
    )
    dane = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": uzytkownik},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 1000,
    }
    naglowki = {"Authorization": f"Bearer {klucz}", "Content-Type": "application/json"}
    wynik = http_post_json(OPENROUTER, dane, naglowki)
    if "choices" not in wynik:
        raise RuntimeError(f"nieoczekiwana odpowiedź OpenRouter: {json.dumps(wynik)[:300]}")
    s = wyciagnij_json(wynik["choices"][0]["message"]["content"])
    try:
        s["trafnosc"] = int(s.get("trafnosc", 0))
    except (TypeError, ValueError):
        s["trafnosc"] = 0
    if s.get("typ_badania") not in DOZWOLONE_ETYKIETY:
        s["typ_badania"] = "Inne"
    for pole in ("tytul_pl", "streszczenie", "metoda", "wynik", "ograniczenia", "wniosek_dla_praktyki"):
        s[pole] = str(s.get(pole) or "nie podano w abstrakcie").strip()
    return s


# ---------------------------- Kanał RSS -------------------------------

def tresc_html(w):
    e = html.escape
    link = w["link"]
    zrodlo = ", ".join(x for x in (w.get("autor"), w.get("czasopismo")) if x)
    return (
        f"<p><strong>{e(w['typ_badania'])}</strong> · trafność {w['trafnosc']}/10</p>"
        f"<p>{e(w['streszczenie'])}</p>"
        f"<p><strong>Metoda:</strong> {e(w['metoda'])}</p>"
        f"<p><strong>Wynik:</strong> {e(w['wynik'])}</p>"
        f"<p><strong>Ograniczenia:</strong> {e(w['ograniczenia'])}</p>"
        f"<p><strong>Wniosek dla praktyki:</strong> {e(w['wniosek_dla_praktyki'])}</p>"
        f"<p><em>Tytuł oryginalny:</em> {e(w['tytul'])}<br/>"
        f"<em>Źródło:</em> {e(zrodlo)}<br/>"
        f"<a href=\"{e(link)}\">Czytaj oryginał</a></p>"
        f"<p><small>Streszczenie wygenerowane przez AI na podstawie abstraktu. "
        f"Przed zastosowaniem w praktyce sprawdź artykuł źródłowy.</small></p>"
    )


def adres_strony():
    repo = os.environ.get("GITHUB_REPOSITORY", "")  # np. "login/psychoterapia-rss"
    if "/" in repo:
        wlasciciel, nazwa = repo.split("/", 1)
        return f"https://{wlasciciel.lower()}.github.io/{nazwa}/"
    return "https://pubmed.ncbi.nlm.nih.gov/"


def zapisz_kanal(wpisy):
    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")
    rss = ET.Element("rss", {"version": "2.0"})
    kanal = ET.SubElement(rss, "channel")
    strona = adres_strony()
    ET.SubElement(kanal, "title").text = TYTUL_KANALU
    ET.SubElement(kanal, "link").text = strona
    ET.SubElement(kanal, "description").text = OPIS_KANALU
    ET.SubElement(kanal, "language").text = "pl"
    ET.SubElement(kanal, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    if "github.io" in strona:
        ET.SubElement(kanal, "{http://www.w3.org/2005/Atom}link", {
            "href": strona + PLIK_KANALU, "rel": "self", "type": "application/rss+xml"})
    for w in wpisy:
        item = ET.SubElement(kanal, "item")
        ET.SubElement(item, "title").text = f"[{w['typ_badania']}] {w['tytul_pl']}"
        ET.SubElement(item, "link").text = w["link"]
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = "pmid:" + w["pmid"]
        ET.SubElement(item, "pubDate").text = w["dodano"]
        ET.SubElement(item, "description").text = tresc_html(w)
    ET.indent(rss)
    ET.ElementTree(rss).write(PLIK_KANALU, encoding="utf-8", xml_declaration=True)


# ---------------------------- Główna część ----------------------------

def wczytaj_stan():
    if os.path.exists(PLIK_STANU):
        with open(PLIK_STANU, encoding="utf-8") as f:
            return json.load(f)
    return {"widziane": [], "wpisy": []}


def main():
    klucz = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not klucz:
        sys.exit("BŁĄD: brak klucza OPENROUTER_API_KEY. Dodaj go w Settings → Secrets and variables → Actions.")

    stan = wczytaj_stan()
    widziane = set(stan["widziane"])

    print("Szukam nowych artykułów w PubMed...")
    pmidy = [p for p in szukaj_pubmed() if p not in widziane]
    print(f"  nowych do sprawdzenia: {len(pmidy)}")

    artykuly = pobierz_artykuly(pmidy) if pmidy else []
    artykuly = [a for a in artykuly if a["pmid"] not in widziane]
    pominiete = [a for a in artykuly if POMIJANE_TYPY & set(a["typy"])]
    artykuly = [a for a in artykuly if not (POMIJANE_TYPY & set(a["typy"]))]
    for a in pominiete:
        stan["widziane"].append(a["pmid"])
    artykuly.sort(key=ranga)  # sortowanie stabilne: w ramach typu zostaje kolejność od najnowszych
    kandydaci = artykuly[:MAKS_KANDYDATOW]
    print(f"  oceniam modelem {MODEL}: {len(kandydaci)} artykułów")

    ocenione, bledy = [], 0
    for i, a in enumerate(kandydaci, 1):
        try:
            s = ocen_i_streszcz(a, klucz)
        except Exception as e:  # jeden nieudany artykuł nie zatrzymuje reszty
            bledy += 1
            print(f"  [{i}/{len(kandydaci)}] BŁĄD: {e}")
            if bledy >= 5 and not ocenione:
                sys.exit("BŁĄD: model nie odpowiada poprawnie. Sprawdź klucz, saldo na OpenRouter i nazwę modelu.")
            continue
        stan["widziane"].append(a["pmid"])
        print(f"  [{i}/{len(kandydaci)}] trafność {s['trafnosc']}: {a['tytul'][:70]}")
        if s["trafnosc"] >= MIN_OCENA:
            link = f"https://doi.org/{a['doi']}" if a["doi"] else f"https://pubmed.ncbi.nlm.nih.gov/{a['pmid']}/"
            ocenione.append({**s, "pmid": a["pmid"], "tytul": a["tytul"], "autor": a["autor"],
                             "czasopismo": a["czasopismo"], "link": link})

    ocenione.sort(key=lambda w: -w["trafnosc"])
    nowe = ocenione[:MAKS_NOWYCH]
    teraz = format_datetime(datetime.now(timezone.utc))
    for w in nowe:
        w["dodano"] = teraz

    stan["wpisy"] = (nowe + stan["wpisy"])[:MAKS_W_KANALE]
    stan["widziane"] = stan["widziane"][-5000:]
    with open(PLIK_STANU, "w", encoding="utf-8") as f:
        json.dump(stan, f, ensure_ascii=False, indent=1)
    zapisz_kanal(stan["wpisy"])
    print(f"Gotowe. Dodano {len(nowe)} wpisów, w kanale jest {len(stan['wpisy'])}.")

    if kandydaci and bledy == len(kandydaci):
        sys.exit("BŁĄD: żaden artykuł nie został oceniony przez model.")


if __name__ == "__main__":
    main()
