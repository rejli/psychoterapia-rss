#!/usr/bin/env python3
"""
Bot RSS: nowe badania z psychoterapii.

Co robi przy każdym uruchomieniu:
1. Pobiera nowe prace z czterech źródeł:
   - PubMed (artykuły z ostatnich dni),
   - OpenAlex (czasopisma spoza PubMed),
   - Europe PMC (preprinty, m.in. PsyArXiv),
   - kanały RSS z doniesieniami popularnonaukowymi.
2. Wysyła abstrakt / opis każdej pracy do modelu AI (przez OpenRouter),
   który ocenia trafność i pisze streszczenie po polsku.
3. Usuwa duplikaty między źródłami (po DOI, a gdy brak - po tytule)
   i dopisuje najlepsze pozycje do pliku feed.xml (kanał RSS).

Nie wymaga instalowania żadnych dodatkowych bibliotek.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
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
    '"gestalt therapy"[Title/Abstract]',
    '"gestalt psychotherapy"[Title/Abstract]',
]

# Te same tematy, ale jako zwykłe frazy - dla źródeł spoza PubMed
# (OpenAlex, Europe PMC), które nie rozumieją znaczników PubMed.
TEMATY_PROSTE = [
    "psychotherapy",
    "cognitive behavioral therapy",
    "cognitive behavioural therapy",
    "schema therapy",
    "psychodynamic psychotherapy",
    "EMDR",
    "dialectical behavior therapy",
    "acceptance and commitment therapy",
    "mentalization-based therapy",
    "therapeutic alliance",
    "psychological intervention",
    "psychological treatment",
    "gestalt therapy",
    "gestalt psychotherapy",
]

# Kanały RSS z doniesieniami popularnonaukowymi o badaniach z psychologii.
# (nazwa pokazywana we wpisie, adres kanału)
KANALY_DONIESIEN = [
    ("ScienceDaily – Psychologia", "https://www.sciencedaily.com/rss/mind_brain/psychology.xml"),
    ("PsyPost", "https://www.psypost.org/feed/"),
]

# Opis Twoich zainteresowań - model ocenia trafność względem tego opisu.
ZAINTERESOWANIA = (
    "Psychoterapia i psychologia kliniczna: skuteczność różnych nurtów terapii, "
    "badania nad procesem terapii i relacją terapeutyczną, czynniki leczące, "
    "nowe interwencje psychologiczne, terapia zaburzeń psychicznych u dorosłych, "
    "dzieci i młodzieży."
)

DNI_WSTECZ = 3          # z ilu ostatnich dni brać artykuły z PubMed
DNI_WSTECZ_INNE = 7     # z ilu ostatnich dni brać prace z OpenAlex i preprinty
MAKS_KANDYDATOW = 40    # ile artykułów z PubMed najwyżej oceniać modelem
MIN_OCENA = 6           # minimalna trafność (1-10), żeby artykuł trafił do kanału
MAKS_NOWYCH = 12        # ile najwyżej nowych wpisów (recenzowanych) na uruchomienie
MAKS_W_KANALE = 200     # ile wpisów trzymać w kanale

# Dodatkowe źródła - osobne, niewielkie limity, żeby nie wypierały
# recenzowanych badań z PubMed/OpenAlex.
MAKS_KANDYDATOW_OPENALEX = 8    # ile prac z czasopism spoza PubMed oceniać modelem
MAKS_KANDYDATOW_PREPRINTY = 6   # ile preprintów oceniać modelem
MAKS_DONIESIEN_OCENA = 6        # ile doniesień RSS oceniać modelem
MAKS_NOWYCH_PREPRINTY = 3       # ile najwyżej nowych preprintów na uruchomienie
MAKS_NOWYCH_DONIESIEN = 3       # ile najwyżej nowych doniesień na uruchomienie
MIN_OCENA_DONIESIENIE = 6       # minimalna trafność doniesienia, żeby trafiło do kanału
# Łączny budżet zapytań do modelu na uruchomienie: 40 + 8 + 6 + 6 = maks. 60.

# Kontakt dla "grzecznej puli" OpenAlex (opcjonalny). Zostaw pusty, działa i tak.
OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO", "").strip()

TYTUL_KANALU = "Psychoterapia – nowe badania"
OPIS_KANALU = "Automatyczny przegląd nowych badań z psychoterapii i psychologii klinicznej, streszczany przez AI."

# ----------------------------------------------------------------------
# Dalej jest już sam mechanizm - nie trzeba nic zmieniać
# ----------------------------------------------------------------------

PLIK_STANU = "state.json"
PLIK_KANALU = "feed.xml"
PUBMED = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
OPENALEX = "https://api.openalex.org/"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/"
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


# ------------------------ Odduplikowanie ------------------------------

def znormalizuj_doi(doi):
    """DOI do porównań: małe litery, bez prefiksu i bez sufiksu wersji preprintu."""
    d = (doi or "").strip().lower()
    for prefiks in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(prefiks):
            d = d[len(prefiks):]
    return re.sub(r"_v\d+$", "", d).strip()


def doi_do_linku(doi):
    """DOI do linku (zachowuje wielkość liter, usuwa tylko prefiks)."""
    d = (doi or "").strip()
    for prefiks in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.lower().startswith(prefiks):
            d = d[len(prefiks):]
    return d


def znormalizuj_tytul(tytul):
    return re.sub(r"[^a-z0-9]+", "", (tytul or "").lower())


def klucz_pozycji(doi, tytul):
    """Uniwersalny klucz do odduplikowania: po DOI, a gdy brak - po tytule."""
    nd = znormalizuj_doi(doi)
    if nd:
        return "doi:" + nd
    nt = znormalizuj_tytul(tytul)
    return "tytul:" + nt if nt else ""


def wybierz_kandydatow(artykuly, limit, widziane_pmid, uzyte_klucze):
    """Z listy prac wybiera do oceny modelem te, których jeszcze nie widzieliśmy
    (ani w stanie, ani w tym uruchomieniu z innego źródła)."""
    wybrani = []
    for a in artykuly:
        if len(wybrani) >= limit:
            break
        if a.get("pmid") and a["pmid"] in widziane_pmid:
            continue
        k = klucz_pozycji(a.get("doi", ""), a.get("tytul", ""))
        if not k or k in uzyte_klucze:
            continue
        uzyte_klucze.add(k)
        a["klucz"] = k
        wybrani.append(a)
    return wybrani


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
        "zrodlo": "PubMed", "rodzaj": "badanie",
    }


def ranga(artykul):
    for i, typ in enumerate(PRIORYTET_TYPOW):
        if typ in artykul["typy"]:
            return i
    return len(PRIORYTET_TYPOW)


# ---------------- OpenAlex: czasopisma spoza PubMed -------------------

def abstrakt_z_indeksu(inv):
    """OpenAlex podaje abstrakt jako odwrócony indeks słów - składamy tekst."""
    if not inv:
        return ""
    slowa = []
    for slowo, pozycje in inv.items():
        for i in pozycje:
            slowa.append((i, slowo))
    slowa.sort()
    return " ".join(s for _, s in slowa)


def szukaj_openalex():
    od = (datetime.now(timezone.utc).date() - timedelta(days=DNI_WSTECZ_INNE)).isoformat()
    szukaj = " OR ".join(f'"{t}"' for t in TEMATY_PROSTE)
    filtr = (f"title_and_abstract.search:{szukaj},type:article,"
             f"has_abstract:true,from_publication_date:{od}")
    parametry = {"filter": filtr, "sort": "publication_date:desc", "per_page": 50}
    if OPENALEX_MAILTO:
        parametry["mailto"] = OPENALEX_MAILTO
    dane = json.loads(http_get(OPENALEX + "works?" + urllib.parse.urlencode(parametry)))
    artykuly = []
    for w in dane.get("results", []):
        if (w.get("ids") or {}).get("pmid"):
            continue  # praca jest już w PubMed - pomijamy, bierze ją tamto źródło
        tytul = (w.get("title") or "").strip()
        abstrakt = abstrakt_z_indeksu(w.get("abstract_inverted_index")).strip()
        if not (tytul and abstrakt):
            continue
        lokacja = w.get("primary_location") or {}
        czasopismo = (lokacja.get("source") or {}).get("display_name") or ""
        autorzy = []
        for a in w.get("authorships", []):
            nazwa = (a.get("author") or {}).get("display_name") or ""
            if nazwa:
                autorzy.append(nazwa.split()[-1])
        if len(autorzy) > 2:
            autor = f"{autorzy[0]} i in."
        else:
            autor = " i ".join(autorzy)
        typy = ["Review"] if w.get("type") == "review" else []
        artykuly.append({
            "pmid": "", "tytul": tytul, "abstrakt": abstrakt, "typy": typy,
            "czasopismo": czasopismo, "doi": (w.get("doi") or "").strip(), "autor": autor,
            "link_zap": lokacja.get("landing_page_url") or w.get("id") or "",
            "zrodlo": "OpenAlex (czasopismo spoza PubMed)", "rodzaj": "badanie",
        })
    return artykuly


# ---------------- Europe PMC: preprinty (m.in. PsyArXiv) --------------

def szukaj_preprinty():
    szukaj = " OR ".join(f'"{t}"' for t in TEMATY_PROSTE)
    zapytanie = f"({szukaj}) AND SRC:PPR AND HAS_ABSTRACT:Y"
    parametry = urllib.parse.urlencode({
        "query": zapytanie, "format": "json", "resultType": "core",
        "pageSize": 30, "sort": "P_PDATE_D desc",
    })
    dane = json.loads(http_get(EUROPEPMC + "search?" + parametry))
    granica = (datetime.now(timezone.utc).date() - timedelta(days=DNI_WSTECZ_INNE)).isoformat()
    artykuly = []
    for r in dane.get("resultList", {}).get("result", []):
        data_pub = (r.get("firstPublicationDate") or "")[:10]
        if data_pub and data_pub < granica:
            continue  # starszy niż okno - pomijamy
        tytul = re.sub(r"\s+", " ", (r.get("title") or "")).strip().rstrip(".")
        abstrakt = re.sub(r"<[^>]+>", " ", r.get("abstractText") or "")
        abstrakt = re.sub(r"\s+", " ", abstrakt).strip()
        if not (tytul and abstrakt):
            continue
        serwer = (r.get("bookOrReportDetails") or {}).get("publisher") or "serwer preprintów"
        autor = r.get("authorString") or ""
        if autor.count(",") >= 2:
            autor = autor.split(",")[0].strip() + " i in."
        artykuly.append({
            "pmid": "", "tytul": tytul, "abstrakt": abstrakt, "typy": [],
            "czasopismo": serwer, "doi": (r.get("doi") or "").strip(), "autor": autor,
            "link_zap": f"https://doi.org/{doi_do_linku(r.get('doi') or '')}" if r.get("doi") else "",
            "zrodlo": f"Preprint ({serwer})", "rodzaj": "preprint",
        })
    return artykuly


# ---------------- Doniesienia popularnonaukowe (RSS) -----------------

def pobierz_doniesienia():
    """Pobiera pozycje z kanałów RSS i przeplata je (po równo z każdego kanału),
    żeby każdy kanał miał szansę w limicie ocen."""
    listy = []
    for nazwa, url in KANALY_DONIESIEN:
        try:
            root = ET.fromstring(http_get(url))
            pozycje = []
            for item in root.findall(".//item"):
                tytul = tekst(item.find("title"))
                link = tekst(item.find("link"))
                opis = re.sub(r"<[^>]+>", " ", tekst(item.find("description")))
                opis = re.sub(r"\s+", " ", opis).strip()
                if tytul and link and opis:
                    pozycje.append({
                        "tytul": tytul, "opis": opis[:1500], "link": link,
                        "doi": "", "zrodlo": nazwa, "rodzaj": "doniesienie",
                    })
            listy.append(pozycje)
        except Exception as e:  # awaria jednego kanału nie blokuje reszty
            print(f"  [doniesienia] BŁĄD kanału {nazwa}: {e}")
    wynik, i = [], 0
    while any(i < len(l) for l in listy):
        for l in listy:
            if i < len(l):
                wynik.append(l[i])
        i += 1
    return wynik


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


INSTRUKCJA_DONIESIENIE = """Jesteś redaktorem, który po polsku streszcza KRÓTKĄ NOTKĘ PRASOWĄ o badaniu z psychologii.
UWAGA: dostajesz wyłącznie krótki opis prasowy z kanału informacyjnego, a NIE abstrakt naukowy.

Zasady:
- Opieraj się WYŁĄCZNIE na podanym opisie. Nie dodawaj żadnych liczb, wielkości próby ani wyników, których w opisie nie ma.
- NIE przypisuj typu badania (nie pisz, że to RCT, metaanaliza, przegląd itp.), nawet jeśli się domyślasz.
- Pisz zwięźle i ostrożnie. To doniesienie popularnonaukowe, nie recenzowany artykuł.

Zainteresowania czytelnika: {zainteresowania}

Zwróć wyłącznie obiekt JSON z polami:
- "trafnosc": liczba całkowita 1-10. Jak bardzo doniesienie dotyczy psychoterapii/psychologii klinicznej i jest warte uwagi czytelnika.
- "tytul_pl": tytuł doniesienia przetłumaczony na polski
- "streszczenie": 2-3 zdania po polsku: czego dotyczy doniesienie, wyłącznie na podstawie opisu"""


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


def streszcz_doniesienie(poz, klucz):
    system = INSTRUKCJA_DONIESIENIE.format(zainteresowania=ZAINTERESOWANIA)
    uzytkownik = (
        f"Tytuł: {poz['tytul']}\n"
        f"Kanał: {poz['zrodlo']}\n\n"
        f"Opis z kanału:\n{poz['opis']}"
    )
    dane = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": uzytkownik},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 500,
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
    s["tytul_pl"] = str(s.get("tytul_pl") or poz["tytul"]).strip()
    s["streszczenie"] = str(s.get("streszczenie") or "").strip()
    return s


# --------------- Ocena list prac i doniesień modelem ------------------

def ocen_liste(kandydaci, klucz, stan, widziane_pmid, licznik):
    """Ocenia prace (PubMed/OpenAlex/preprinty), zwraca wpisy z trafnością >= MIN_OCENA.
    licznik to [proby, bledy] - wspólny budżet i statystyka całego uruchomienia."""
    wpisy = []
    for i, a in enumerate(kandydaci, 1):
        licznik[0] += 1
        try:
            s = ocen_i_streszcz(a, klucz)
        except Exception as e:  # jeden nieudany artykuł nie zatrzymuje reszty
            licznik[1] += 1
            print(f"  [{a['zrodlo']}] [{i}/{len(kandydaci)}] BŁĄD: {e}")
            continue
        if a.get("pmid"):
            stan["widziane"].append(a["pmid"])
            widziane_pmid.add(a["pmid"])
        stan["widziane_klucze"].append(a["klucz"])
        print(f"  [{a['zrodlo']}] [{i}/{len(kandydaci)}] trafność {s['trafnosc']}: {a['tytul'][:60]}")
        if s["trafnosc"] >= MIN_OCENA:
            if a.get("doi"):
                link = "https://doi.org/" + doi_do_linku(a["doi"])
            elif a.get("pmid"):
                link = f"https://pubmed.ncbi.nlm.nih.gov/{a['pmid']}/"
            else:
                link = a.get("link_zap", "")
            wpisy.append({**s, "pmid": a.get("pmid", ""), "tytul": a["tytul"],
                          "autor": a.get("autor", ""), "czasopismo": a.get("czasopismo", ""),
                          "link": link, "zrodlo": a["zrodlo"], "rodzaj": a["rodzaj"],
                          "guid": a["klucz"], "klucz": a["klucz"]})
    return wpisy


def ocen_doniesienia(pozycje, klucz, stan, uzyte_klucze, licznik):
    """Ocenia doniesienia RSS (osobny, prostszy prompt). Limit = MAKS_DONIESIEN_OCENA prób."""
    wpisy, proby = [], 0
    for p in pozycje:
        if proby >= MAKS_DONIESIEN_OCENA:
            break
        k = klucz_pozycji("", p["tytul"])
        if not k or k in uzyte_klucze:
            continue
        uzyte_klucze.add(k)
        proby += 1
        licznik[0] += 1
        try:
            s = streszcz_doniesienie(p, klucz)
        except Exception as e:
            licznik[1] += 1
            print(f"  [{p['zrodlo']}] BŁĄD: {e}")
            continue
        stan["widziane_klucze"].append(k)
        print(f"  [{p['zrodlo']}] trafność {s['trafnosc']}: {p['tytul'][:60]}")
        if s["trafnosc"] >= MIN_OCENA_DONIESIENIE:
            wpisy.append({"trafnosc": s["trafnosc"], "tytul_pl": s["tytul_pl"],
                          "streszczenie": s["streszczenie"], "tytul": p["tytul"],
                          "link": p["link"], "zrodlo": p["zrodlo"], "rodzaj": "doniesienie",
                          "autor": "", "czasopismo": "", "guid": k, "klucz": k})
    return wpisy


# ---------------------------- Kanał RSS -------------------------------

def tresc_html(w):
    e = html.escape
    link = w["link"]
    if w.get("rodzaj") == "doniesienie":
        return (
            f"<p><strong>Doniesienie popularnonaukowe</strong> · trafność {w['trafnosc']}/10</p>"
            f"<p>{e(w['streszczenie'])}</p>"
            f"<p><small><strong>Uwaga:</strong> to streszczenie krótkiej notki prasowej z kanału "
            f"„{e(w.get('zrodlo', ''))}”, a NIE abstraktu badania. Nie zawiera metody ani wyników "
            f"liczbowych – te znajdziesz dopiero w tekście źródłowym.</small></p>"
            f"<p><em>Tytuł oryginalny:</em> {e(w['tytul'])}<br/>"
            f"<em>Źródło danych:</em> {e(w.get('zrodlo', ''))}<br/>"
            f"<a href=\"{e(link)}\">Czytaj źródło</a></p>"
            f"<p><small>Streszczenie wygenerowane przez AI na podstawie opisu z kanału RSS.</small></p>"
        )
    zrodlo = ", ".join(x for x in (w.get("autor"), w.get("czasopismo")) if x)
    baner = ""
    if w.get("rodzaj") == "preprint":
        baner = ("<p><strong>Preprint – praca nierecenzowana.</strong> Nie przeszła recenzji "
                 "naukowej; wyniki są wstępne i mogą się zmienić.</p>")
    return (
        baner
        + f"<p><strong>{e(w['typ_badania'])}</strong> · trafność {w['trafnosc']}/10</p>"
        f"<p>{e(w['streszczenie'])}</p>"
        f"<p><strong>Metoda:</strong> {e(w['metoda'])}</p>"
        f"<p><strong>Wynik:</strong> {e(w['wynik'])}</p>"
        f"<p><strong>Ograniczenia:</strong> {e(w['ograniczenia'])}</p>"
        f"<p><strong>Wniosek dla praktyki:</strong> {e(w['wniosek_dla_praktyki'])}</p>"
        f"<p><em>Tytuł oryginalny:</em> {e(w['tytul'])}<br/>"
        f"<em>Źródło:</em> {e(zrodlo)}<br/>"
        f"<em>Źródło danych:</em> {e(w.get('zrodlo', 'PubMed'))}<br/>"
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
        if w.get("rodzaj") == "doniesienie":
            etykieta = "Doniesienie"
        elif w.get("rodzaj") == "preprint":
            etykieta = "Preprint"
        else:
            etykieta = w["typ_badania"]
        ET.SubElement(item, "title").text = f"[{etykieta}] {w['tytul_pl']}"
        ET.SubElement(item, "link").text = w["link"]
        guid = w.get("guid") or ("pmid:" + w.get("pmid", ""))
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = guid
        ET.SubElement(item, "pubDate").text = w["dodano"]
        ET.SubElement(item, "description").text = tresc_html(w)
    ET.indent(rss)
    ET.ElementTree(rss).write(PLIK_KANALU, encoding="utf-8", xml_declaration=True)


# ---------------------------- Główna część ----------------------------

def wczytaj_stan():
    if os.path.exists(PLIK_STANU):
        with open(PLIK_STANU, encoding="utf-8") as f:
            stan = json.load(f)
    else:
        stan = {"widziane": [], "wpisy": []}
    stan.setdefault("widziane", [])
    stan.setdefault("wpisy", [])
    stan.setdefault("widziane_klucze", [])  # klucze DOI/tytuł dla źródeł spoza PubMed
    return stan


def zbierz_pubmed(widziane_pmid, uzyte_klucze, stan):
    """Pobiera i wstępnie filtruje artykuły z PubMed (jak dotychczas)."""
    print("Źródło: PubMed...")
    pmidy = [p for p in szukaj_pubmed() if p not in widziane_pmid]
    artykuly = pobierz_artykuly(pmidy) if pmidy else []
    pominiete = [a for a in artykuly if POMIJANE_TYPY & set(a["typy"])]
    artykuly = [a for a in artykuly if not (POMIJANE_TYPY & set(a["typy"]))]
    for a in pominiete:  # pomijane typy (listy, errata...) od razu oznaczamy jako widziane
        stan["widziane"].append(a["pmid"])
        widziane_pmid.add(a["pmid"])
    artykuly.sort(key=ranga)  # pierwszeństwo: metaanalizy, przeglądy, RCT
    kandydaci = wybierz_kandydatow(artykuly, MAKS_KANDYDATOW, widziane_pmid, uzyte_klucze)
    print(f"  znaleziono: {len(artykuly)}, do oceny: {len(kandydaci)}")
    return len(artykuly), kandydaci


def bezpiecznie(nazwa, funkcja):
    """Uruchamia pobieranie ze źródła; awaria nie zatrzymuje pozostałych źródeł."""
    try:
        return funkcja()
    except Exception as e:
        print(f"  [{nazwa}] BŁĄD źródła, pomijam: {e}")
        return []


def main():
    klucz = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not klucz:
        sys.exit("BŁĄD: brak klucza OPENROUTER_API_KEY. Dodaj go w Settings → Secrets and variables → Actions.")

    stan = wczytaj_stan()
    widziane_pmid = set(stan["widziane"])
    uzyte_klucze = set(stan["widziane_klucze"])  # dedup między źródłami i między uruchomieniami
    licznik = [0, 0]  # [zapytania do modelu, błędy]
    statystyki = {}

    # --- Zbieranie kandydatów z każdego źródła (awaria jednego nie blokuje reszty) ---
    znal_pubmed, kand_pubmed = bezpiecznie(
        "PubMed", lambda: zbierz_pubmed(widziane_pmid, uzyte_klucze, stan)) or (0, [])

    print("Źródło: OpenAlex (czasopisma spoza PubMed)...")
    art_openalex = bezpiecznie("OpenAlex", szukaj_openalex)
    kand_openalex = wybierz_kandydatow(art_openalex, MAKS_KANDYDATOW_OPENALEX, widziane_pmid, uzyte_klucze)
    print(f"  znaleziono: {len(art_openalex)}, do oceny: {len(kand_openalex)}")

    print("Źródło: Preprinty (Europe PMC / PsyArXiv)...")
    art_pre = bezpiecznie("Preprinty", szukaj_preprinty)
    kand_pre = wybierz_kandydatow(art_pre, MAKS_KANDYDATOW_PREPRINTY, widziane_pmid, uzyte_klucze)
    print(f"  znaleziono: {len(art_pre)}, do oceny: {len(kand_pre)}")

    print("Źródło: Doniesienia (RSS)...")
    poz_doniesienia = bezpiecznie("Doniesienia", pobierz_doniesienia)
    print(f"  znaleziono pozycji: {len(poz_doniesienia)}")

    # --- Ocena modelem (wspólny budżet, maks. ok. 60 zapytań) ---
    print(f"Oceniam modelem {MODEL}...")
    wpisy_pubmed = ocen_liste(kand_pubmed, klucz, stan, widziane_pmid, licznik)
    wpisy_openalex = ocen_liste(kand_openalex, klucz, stan, widziane_pmid, licznik)
    wpisy_pre = ocen_liste(kand_pre, klucz, stan, widziane_pmid, licznik)
    wpisy_don = ocen_doniesienia(poz_doniesienia, klucz, stan, uzyte_klucze, licznik)

    # Jeśli model w ogóle nie odpowiedział (klucz/saldo/model) - nie obchodzimy tego.
    if licznik[0] > 0 and licznik[1] == licznik[0]:
        sys.exit("BŁĄD: model nie odpowiada poprawnie na żadne zapytanie. "
                 "Sprawdź klucz, saldo na OpenRouter i nazwę modelu (patrz błędy wyżej).")

    # --- Osobne limity dzienne, żeby preprinty i doniesienia nie wypierały badań ---
    recenzowane = sorted(wpisy_pubmed + wpisy_openalex, key=lambda w: -w["trafnosc"])[:MAKS_NOWYCH]
    preprinty = sorted(wpisy_pre, key=lambda w: -w["trafnosc"])[:MAKS_NOWYCH_PREPRINTY]
    doniesienia = sorted(wpisy_don, key=lambda w: -w["trafnosc"])[:MAKS_NOWYCH_DONIESIEN]
    nowe = recenzowane + preprinty + doniesienia

    teraz = format_datetime(datetime.now(timezone.utc))
    for w in nowe:
        w["dodano"] = teraz

    stan["wpisy"] = (nowe + stan["wpisy"])[:MAKS_W_KANALE]
    stan["widziane"] = stan["widziane"][-5000:]
    stan["widziane_klucze"] = stan["widziane_klucze"][-8000:]
    with open(PLIK_STANU, "w", encoding="utf-8") as f:
        json.dump(stan, f, ensure_ascii=False, indent=1)
    zapisz_kanal(stan["wpisy"])

    # --- Podsumowanie dla logu ---
    don_dodane = sum(1 for w in nowe if w["rodzaj"] == "doniesienie")
    pub_dodane = sum(1 for w in recenzowane if w["zrodlo"] == "PubMed")
    oa_dodane = sum(1 for w in recenzowane if w["zrodlo"].startswith("OpenAlex"))
    print("----------------------------------------------------------")
    print("PODSUMOWANIE ŹRÓDEŁ (znaleziono -> dodano do kanału):")
    print(f"  PubMed:       {znal_pubmed} -> {pub_dodane}")
    print(f"  OpenAlex:     {len(art_openalex)} -> {oa_dodane}")
    print(f"  Preprinty:    {len(art_pre)} -> {len(preprinty)}")
    print(f"  Doniesienia:  {len(poz_doniesienia)} -> {don_dodane}")
    print(f"Zapytań do modelu: {licznik[0]} (błędów: {licznik[1]}).")
    print(f"Gotowe. Dodano łącznie {len(nowe)} wpisów, w kanale jest {len(stan['wpisy'])}.")


if __name__ == "__main__":
    main()
