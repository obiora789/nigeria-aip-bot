#!/usr/bin/env python3
"""
vectorise_aip_v2.py — Nigerian AIP 2026 Complete Text Vectorization
====================================================================
Run order: extract_charts.py FIRST, then this script. This script reads
aip_charts.source_page to EXCLUDE chart plate pages from the text index (their
text layer is flattened diagram annotations, dangerous as prose). If aip_charts
is empty it aborts unless ALLOW_NO_CHART_EXCLUSION=1.

Pre-requisites:
  1. Run schema_updates.sql in Supabase SQL Editor
  2. Populate aip_charts (run extract_charts.py) so plate pages are known
  3. Confirm aip_knowledge_base is empty: DELETE FROM aip_knowledge_base;
     and delete vectorise_v2_progress.json for a clean re-ingest
  4. Set environment variables in .env

Section titles sourced directly from AIP table of contents:
  • GEN TOC: page 22
  • ENR TOC: page 122
  • AD TOC:  pages 304–319

Improvements over previous version:
  • Complete SECTION_TITLES from TOC — all 60 sections correctly named
  • All AD 2.x sub-sections (AD 2.1–2.24) — critical for aerodrome retrieval
  • Expanded ACCUMULATE_SECTIONS — ENR 3 (airways) and ENR 5 (danger areas)
    added alongside ENR 2 and ENR 4
  • Enrichment keyword prefixes per section family
  • Context-prefixed chunks, sentence-boundary chunking, infinite-loop guard,
    OpenAI timeout/retry, progress file, upsert safety — all retained from v2
"""

import os, re, json, time
import fitz
from openai import OpenAI
from supabase import create_client
from dotenv import load_dotenv
from extract_page_text_fixed import extract_page_text

load_dotenv()

client   = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=30.0, max_retries=0)
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

PDF_PATH      = os.getenv("PDF_PATH")
TABLE_NAME    = "aip_knowledge_base"
CHUNK_SIZE    = 1500
CHUNK_OVERLAP = 300
RATE_PAUSE    = 0.35
PROGRESS_FILE = "vectorise_v2_progress.json"

# ── SECTION TITLES ────────────────────────────────────────────────────────────
# Sourced from the three AIP Tables of Contents:
#   GEN 0.6 TABLE OF CONTENTS TO PART 1  — page 22
#   ENR 0.6 TABLE OF CONTENTS TO PART 2  — page 122
#   AD  0.6 TABLE OF CONTENTS TO PART 3  — pages 304–319

SECTION_TITLES = {

    # ── GEN — Part 1 ──────────────────────────────────────────────────────────
    "GEN 0.1": "Preface",
    "GEN 0.2": "Record of AIP Amendments",
    "GEN 0.3": "Record of AIP Supplements",
    "GEN 0.4": "Checklist of AIP Pages",
    "GEN 0.5": "List of Hand Amendments",
    "GEN 0.6": "Table of Contents Part 1",
    "GEN 1":   "National Regulations and Requirements",
    "GEN 1.1": "Designated Authorities",
    "GEN 1.2": "Entry Transit and Departure of Aircraft",
    "GEN 1.3": "Entry Transit and Departure of Passengers and Crew",
    "GEN 1.4": "Entry Transit and Departure of Cargo",
    "GEN 1.5": "Aircraft Instruments Equipment and Flight Documents",
    "GEN 1.6": "Summary of National Regulations and International Agreements",
    "GEN 1.7": "Differences from ICAO Standards Recommended Practices and Procedures",
    "GEN 2":   "Tables and Codes",
    "GEN 2.1": "Measuring System Aircraft Markings Public Holidays",
    "GEN 2.2": "Abbreviations Used in AIS Publications",
    "GEN 2.3": "Chart Symbols",
    "GEN 2.4": "Location Indicators ICAO Codes",
    "GEN 2.5": "Radio Navigation Aids Frequencies",
    "GEN 2.6": "Conversion Tables",
    "GEN 2.7": "Sunrise Sunset Tables",
    "GEN 3":   "Services",
    "GEN 3.1": "Aeronautical Information Services",
    "GEN 3.2": "Aeronautical Charts",
    "GEN 3.3": "Air Traffic Services ATS Contacts and Frequencies",
    "GEN 3.4": "Telecommunication Services",
    "GEN 3.5": "Meteorological Services TAF METAR",
    "GEN 3.6": "Search and Rescue SAR",
    "GEN 4":   "Charges for Aerodromes and Air Navigation Services",
    "GEN 4.1": "Aerodrome Charges",
    "GEN 4.2": "Air Navigation Services Charges",

    # ── ENR — Part 2 ──────────────────────────────────────────────────────────
    "ENR 0.6": "Table of Contents Part 2",
    "ENR 1":   "General Rules and Procedures",
    "ENR 1.1": "General Rules",
    "ENR 1.2": "Visual Flight Rules VFR",
    "ENR 1.3": "Instrument Flight Rules IFR",
    "ENR 1.4": "ATS Airspace Classification",
    "ENR 1.5": "Holding Approach and Departure Procedures",
    "ENR 1.6": "Radar Services and Procedures",
    "ENR 1.7": "Altimeter Setting Procedures Transition Altitude",
    "ENR 1.8": "Regional Supplementary Procedures",
    "ENR 1.9": "Air Traffic Flow Management ATFM",
    "ENR 1.10":"Flight Planning",
    "ENR 1.11":"Addressing of Flight Plan Message",
    "ENR 1.12":"Interception of Civil Aircraft",
    "ENR 1.13":"Unlawful Interference",
    "ENR 1.14":"Air Traffic Incidents",
    "ENR 2":   "ATS Airspace",
    "ENR 2.1": "FIR UIR TMA CTR Lateral Limits Vertical Limits",
    "ENR 2.2": "Other Regulated Airspace",
    "ENR 3":   "ATS Routes",
    "ENR 3.1": "Conventional Routes Airways",
    "ENR 3.2": "Area Navigation Routes RNAV",
    "ENR 3.3": "Other Routes",
    "ENR 3.4": "En-Route Holding",
    "ENR 4":   "Radio Navigation Aids Systems",
    "ENR 4.1": "Radio Navigation Aids En-Route VOR NDB DME ILS Frequencies",
    "ENR 4.2": "Special Navigation Systems",
    "ENR 4.3": "Global Navigation Satellite System GNSS",
    "ENR 4.4": "Name-Code Designators for Significant Points and Waypoints",
    "ENR 4.5": "Aeronautical Ground Lights En-Route",
    "ENR 5":   "Navigation Warnings",
    "ENR 5.1": "Prohibited Restricted and Danger Areas",
    "ENR 5.2": "Military Exercise and Training Areas",
    "ENR 5.3": "Other Activities of a Dangerous Nature",
    "ENR 5.4": "Air Navigation Obstacles En-Route",
    "ENR 5.5": "Aerial Sporting and Recreational Activities",
    "ENR 5.6": "Bird Migration and Areas with Sensitive Fauna",
    "ENR 6":   "En-Route Charts Kano FIR ATS Conventional and RNAV Routes",

    # ── AD — Part 3 (aerodrome sub-sections from AD TOC) ─────────────────────
    "AD 0.6": "Table of Contents Part 3",
    "AD 1":   "Aerodrome Introduction Standards and Index",
    "AD 2":   "Individual Aerodrome Data",
    # AD 2.x sub-sections — every aerodrome has these exact sub-sections
    "AD 2.1": "Aerodrome Location Indicator and Name",
    "AD 2.2": "Aerodrome Geographical and Administrative Data",
    "AD 2.3": "Operational Hours",
    "AD 2.4": "Handling Services and Facilities",
    "AD 2.5": "Passenger Facilities",
    "AD 2.6": "Rescue and Fire Fighting Services RFFS",
    "AD 2.7": "Seasonal Availability Clearing",
    "AD 2.8": "Aprons Taxiways and Check Locations Positions Data",
    "AD 2.9": "Surface Movement Guidance and Control System Markings",
    "AD 2.10":"Aerodrome Obstacles",
    "AD 2.11":"Meteorological Information Provided",
    "AD 2.12":"Runway Physical Characteristics Dimensions",
    "AD 2.13":"Declared Distances TORA TODA ASDA LDA",
    "AD 2.14":"Approach and Runway Lighting ILS PAPI VASI",
    "AD 2.15":"Other Lighting Secondary Power Supply",
    "AD 2.16":"Helicopter Landing Area",
    "AD 2.17":"Air Traffic Services Airspace CTR TMA",
    "AD 2.18":"ATS Communication Facilities Frequencies Tower Approach Ground ATIS",
    "AD 2.19":"Radio Navigation and Landing Aids ILS VOR NDB Frequencies",
    "AD 2.20":"Local Traffic Regulations",
    "AD 2.21":"Noise Abatement Procedures",
    "AD 2.22":"Flight Procedures SID STAR Approach Missed Approach",
    "AD 2.23":"Additional Information",
    "AD 2.24":"Charts Related to an Aerodrome",
    "AD 3":   "Heliports",
}

# ── Aerodrome names for context prefix ───────────────────────────────────────
AERODROME_NAMES = {
    "DNAA": "Abuja Nnamdi Azikiwe",         "DNAI": "Uyo Victor Attah",
    "DNAK": "Akure",                          "DNAN": "Umueri Chinua Achebe",
    "DNAS": "Asaba",                          "DNBB": "Bebi Airstrip",
    "DNBC": "Bauchi Tafawa Balewa",           "DNBE": "Benin",
    "DNBK": "Birnin Kebbi Ahmadu Bello",      "DNBY": "Amassoma Bayelsa",
    "DNCA": "Calabar Margaret Ekpo",          "DNDS": "Dutse",
    "DNEN": "Enugu Akanu Ibiam",              "DNES": "Escravos",
    "DNET": "Ado Ekiti",                      "DNFB": "Bonny Finima",
    "DNFD": "Forcados Terminal",              "DNGB": "Gbaran Gas Plant Heliport",
    "DNGO": "Gombe",                          "DNIB": "Ibadan",
    "DNIL": "Ilorin",                         "DNIM": "Owerri Sam Mbakwe",
    "DNJO": "Jos Yakubu Gowon",               "DNKA": "Kaduna New Kaduna",
    "DNKK": "Kano FIR Nigeria Airspace",      "DNKN": "Kano Mallam Aminu Kano",
    "DNKS": "Kashimbila",                     "DNKT": "Katsina Umaru Yaradua",
    "DNMA": "Maiduguri",                      "DNMK": "Makurdi",
    "DNMM": "Lagos Murtala Muhammed",         "DNMN": "Minna",
    "DNOG": "Ogun Gateway Iperu",             "DNPO": "Port Harcourt Obafemi Awolowo",
    "DNPS": "Port Harcourt Helipad",          "DNSK": "Soku Gas Plant Heliport",
    "DNSO": "Sokoto Saddiq Abubakar",         "DNSU": "Osubi",
    "DNWI": "Warri Heliport",                 "DNYO": "Yola",
    "DNZA": "Zaria",
}

# ── Sections to accumulate before chunking ────────────────────────────────────
# These sections contain tabular data where splitting at character boundaries
# loses the association between a table row's label and its values.
#
# ENR 2.x — CTR/TMA/FIR boundary tables
# ENR 3.x — Airways and route tables (track, level, direction per segment)
# ENR 4.x — Radio navigation aid frequency tables
# ENR 5.x — Restricted/danger area and obstacle tables
#
ACCUMULATE_SECTIONS = {
    "ENR 2",  "ENR 2.1", "ENR 2.2",
    "ENR 3",  "ENR 3.1", "ENR 3.2", "ENR 3.3", "ENR 3.4",
    "ENR 4",  "ENR 4.1", "ENR 4.2", "ENR 4.3", "ENR 4.4", "ENR 4.5",
    "ENR 5",  "ENR 5.1", "ENR 5.2", "ENR 5.3", "ENR 5.4", "ENR 5.5", "ENR 5.6",
}

# Enrichment keyword prefixes per section family (injected before table text)
def enr_enrichment(section):
    if section.startswith("ENR 2"):
        return ("CTR TMA FIR UIR LATERAL LIMITS VERTICAL LIMITS "
                "CIRCLE RADIUS NM CENTRED VOR DME AERODROME AIRSPACE: ")
    if section.startswith("ENR 3"):
        return ("ATS ROUTES AIRWAYS CONVENTIONAL RNAV TRACK MAGNETIC "
                "LEVEL FLIGHT LEVEL DIRECTION SEGMENT DISTANCE NM: ")
    if section.startswith("ENR 4"):
        return ("RADIO NAVIGATION AIDS FREQUENCY MHz kHz VOR NDB DME ILS "
                "IDENTIFIER CALLSIGN COVERAGE GNSS WAYPOINT: ")
    if section.startswith("ENR 5"):
        return ("PROHIBITED RESTRICTED DANGER AREA NAVIGATION WARNING "
                "OBSTACLE COORDINATES ALTITUDE UPPER LOWER LIMIT: ")
    return ""

CHART_KEYWORDS = [
    "CHART","APPROACH","AERODROME","DEPARTURE","ARRIVAL",
    "ILS","VOR","RNAV","SID","STAR","ELEV","HEIGHT","ALTITUDE",
    "EN-ROUTE","ENROUTE","ENR 6","GNSS","RNP",
]

HEADER_NOISE = re.compile(
    r'[\w\s\.\-]+-\d+\s+NIGERIA\s+AIP\s+NIGERIAN\s+AIRSPACE\s+MANAGEMENT\s+AGENCY\s*',
    re.IGNORECASE,
)
SECTION_PAT = re.compile(r'(GEN|ENR|AD)\s+\d+(?:\.\d+)?', re.IGNORECASE)
ICAO_PAT    = re.compile(r'\b(DN[A-Z]{2})\b')

# ── Helpers ───────────────────────────────────────────────────────────────────

def strip_header(text):
    return HEADER_NOISE.sub('', text).strip()


def build_prefix(part, section, reference):
    """
    Build hierarchical AIP context prefix prepended to every chunk.
    Uses the complete section title from the TOC-sourced SECTION_TITLES dict.
    """
    title   = SECTION_TITLES.get(section, "")
    ad_name = AERODROME_NAMES.get(reference, "")
    label   = f"[Nigerian AIP 2026 | {part}"
    if section:
        label += f" | {section}"
    if title:
        label += f" — {title}"
    if ad_name:
        label += f" | {ad_name} ({reference})"
    elif reference not in ("NATIONAL", "AIRSPACE"):
        label += f" | {reference}"
    label += "]"
    return label


def chunk_text(text):
    """Sentence-boundary chunking with infinite-loop guard."""
    chunks, start = [], 0
    while start < len(text):
        end   = min(start + CHUNK_SIZE, len(text))
        chunk = text[start:end]
        if end < len(text):
            for sep in ('. ', '\n', '; ', ', '):
                idx = chunk.rfind(sep)
                if idx > CHUNK_SIZE // 2:
                    chunk = text[start: start + idx + 1]
                    end   = start + idx + 1
                    break
        chunk = chunk.strip()
        if len(chunk) > 50:
            chunks.append(chunk)
        next_start = end - CHUNK_OVERLAP
        if next_start <= start:     # infinite-loop guard
            break
        start = next_start
    return chunks


# ── AD subsection-aware chunking ──────────────────────────────────────────────
# AD 2/AD 3 pages are dense two-column tables. Character-based chunking splits a
# subsection header ("AD 2.13 DECLARED DISTANCES") from its value rows, so the
# values never embed near a pilot's query. Instead we accumulate an aerodrome's
# whole AD 2/AD 3 span, then split on the subsection headers so each field
# (2.2 elevation, 2.12 runway, 2.13 declared distances, 2.19 navaids ...) becomes
# one coherent, retrievable chunk that carries its header AND its values.
AD_SUBSECTION_RE = re.compile(r'(?:DN[A-Z]{2}\s+)?AD\s+([23]\.\d{1,2})\b')

# Field term packs steer each subsection's embedding toward the values pilots ask
# for (declared distances, elevation, navaid idents), not just the header word.
_AD_TERMS = {
    "2.2":  "AERODROME ELEVATION REFERENCE TEMPERATURE ARP GEOGRAPHICAL COORDINATES FEET METRES",
    "2.3":  "OPERATIONAL HOURS ATS CUSTOMS FUELLING",
    "2.6":  "RESCUE FIRE FIGHTING SERVICES RFFS CATEGORY",
    "2.8":  "APRONS TAXIWAYS WIDTH SURFACE CHECK LOCATIONS HOLDING POSITIONS",
    "2.10": "AERODROME OBSTACLES OBSTACLE HEIGHT ELEVATION",
    "2.11": "METEOROLOGICAL INFORMATION TAF METAR TREND FORECAST VALIDITY",
    "2.12": "RUNWAY PHYSICAL CHARACTERISTICS DIMENSIONS LENGTH WIDTH PCN STRENGTH TRUE BEARING THRESHOLD ELEVATION SLOPE",
    "2.13": "DECLARED DISTANCES TORA TODA ASDA LDA RUNWAY",
    "2.14": "APPROACH RUNWAY LIGHTING PAPI VASI THRESHOLD",
    "2.17": "ATS AIRSPACE CLASS TRANSITION ALTITUDE LEVEL",
    "2.18": "ATS COMMUNICATION FACILITIES FREQUENCY TOWER APPROACH GROUND ATIS CALLSIGN MHZ",
    "2.19": "RADIO NAVIGATION LANDING AIDS VOR DME ILS NDB LOCALIZER GLIDE PATH IDENTIFIER FREQUENCY MHZ CHANNEL",
    "2.20": "LOCAL AERODROME REGULATIONS",
    "2.22": "FLIGHT PROCEDURES INSTRUMENT APPROACH OCA OCH DECISION ALTITUDE HEIGHT CIRCLING MISSED APPROACH MINIMA",
    "2.23": "ADDITIONAL INFORMATION BIRD CONCENTRATION",
    "2.24": "CHARTS RELATED TO THE AERODROME",
    "3.2":  "HELIPORT ELEVATION REFERENCE TEMPERATURE COORDINATES",
    "3.10": "HELIPORT OBSTACLES",
    "3.12": "TLOF FATO DIMENSIONS SURFACE",
    "3.13": "DECLARED DISTANCES TODAH RTODAH LDAH",
    "3.18": "COMMUNICATION FACILITIES FREQUENCY",
    "3.19": "RADIO NAVIGATION LANDING AIDS",
}


def ad_enrichment(subsec):
    return _AD_TERMS.get(subsec, "")


def ad_subsection_chunks(text):
    """Split an accumulated AD 2/AD 3 aerodrome span into (subsec, body) pairs on
    its 'AD 2.NN' headers. Returns None if no subsection header is found (caller
    then falls back to plain chunk_text)."""
    matches = list(AD_SUBSECTION_RE.finditer(text))
    if not matches:
        return None
    out = []
    for i, m in enumerate(matches):
        start = m.start()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body  = text[start:end].strip()
        if len(body) > 30:
            out.append((m.group(1), body))     # ("2.13", "AD 2.13 DECLARED ...")
    return out or None


def get_embedding(text, retries=4):
    for attempt in range(retries):
        try:
            r = client.embeddings.create(
                input=text[:8000], model="text-embedding-3-small"
            )
            return r.data[0].embedding
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt * 3
            print(f"  ⚠️  Embed error ({e}) — retrying in {wait}s", flush=True)
            time.sleep(wait)


def db_save(part, section, reference, content, embedding, page_num, chunk_idx):
    """Upsert chunk. Falls back to insert if unique constraint not yet created."""
    payload = {
        "aip_part":      part,
        "aip_section":   section,
        "reference_tag": reference,
        "content":       content,
        "embedding":     embedding,
        "chart_url":     None,
        "source_page":   page_num,
        "source_chunk":  chunk_idx,
        "metadata":      {"source": "AIP_2026", "page": page_num, "chunk": chunk_idx},
    }
    try:
        supabase.table(TABLE_NAME).upsert(
            payload, on_conflict="reference_tag,source_page,source_chunk"
        ).execute()
    except Exception:
        try:
            supabase.table(TABLE_NAME).insert(payload).execute()
        except Exception as e:
            print(f"  ⚠️  DB error p{page_num} chunk{chunk_idx}: {e}", flush=True)


# ── Progress ──────────────────────────────────────────────────────────────────

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"done": [], "chunks": 0}


def save_progress(prog):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(prog, f, indent=2)


# ── Chart/plate-page exclusion ────────────────────────────────────────────────
# Chart PLATE pages (approach/aerodrome/SID/STAR plates in AD 2.24, en-route
# plates in ENR 6, the SAR units map in GEN 3.6) are graphics. Their text layer
# is flattened diagram annotations — scale bars, bearing ticks, coordinate grids,
# loose numbers — which is meaningless and DANGEROUS as prose if a pilot reads a
# value out of context. They belong in aip_charts as images, never in the text
# index. The genuine procedure text (minima, missed approach) lives separately in
# AD 2.22; SAR prose lives in the GEN 3.6 text pages — none of those are plates,
# so they are unaffected by this exclusion.
#
# Single source of truth: aip_charts.source_page (1-indexed, same convention as
# this script's source_page = page_num + 1). Built by extract_charts.py, so that
# script MUST have populated aip_charts before this exclusion can apply. For a
# from-scratch rebuild, run extract_charts.py first. Set ALLOW_NO_CHART_EXCLUSION=1
# to bypass the guard (NOT recommended — re-pollutes the text index).

def load_chart_pages():
    pages = set()
    start, span = 0, 1000
    try:
        while True:
            resp = (supabase.table("aip_charts")
                    .select("source_page")
                    .range(start, start + span - 1).execute())
            rows = resp.data or []
            for r in rows:
                p = r.get("source_page")
                if isinstance(p, int) and p > 0:
                    pages.add(p)
            if len(rows) < span:
                break
            start += span
    except Exception as e:
        print(f"⚠️  Could not read aip_charts for plate-page exclusion: {e}", flush=True)
    return pages


# Second-layer guard: catch plate pages NOT catalogued in aip_charts. These two
# strings appear ONLY on chart plates (map scale + the chart legend) and NEVER in
# legitimate AIP text tables (declared distances, COM tables, etc.), so this is
# safe — it will not strip numeric text pages. Marker-based only, on purpose.
_PLATE_MARKERS = ("SCALE 1:", "BEARINGS, TRACKS AND RADIALS",
                  "BEARINGS TRACKS AND RADIALS")

def looks_like_plate(text: str) -> bool:
    up = text.upper()
    return any(m in up for m in _PLATE_MARKERS)


# ── Section accumulator ───────────────────────────────────────────────────────

class Accumulator:
    def __init__(self):
        self.reset()

    def reset(self, part=None, section=None, reference=None):
        self.part, self.section, self.reference = part, section, reference
        self.text, self.pages = "", []

    def active(self):
        return bool(self.text.strip())

    def matches(self, part, section, reference):
        return (self.part == part
                and self.section == section
                and self.reference == reference)

    def add(self, text, page_num):
        self.text += " " + text
        self.pages.append(page_num)

    def flush(self, total_chunks):
        if not self.active():
            return total_chunks
        if self.part == "AD":
            return self._flush_ad(total_chunks)
        prefix    = build_prefix(self.part, self.section, self.reference)
        full_text = f"{prefix}\n\n{self.text.strip()}"
        chunks    = chunk_text(full_text)
        for idx, chunk in enumerate(chunks):
            try:
                emb = get_embedding(chunk)
                db_save(self.part, self.section, self.reference, chunk, emb,
                        self.pages[0] if self.pages else 0, idx)
                total_chunks += 1
                time.sleep(RATE_PAUSE)
            except Exception as e:
                print(f"  ⚠️  Accumulator flush error: {e}", flush=True)
        print(
            f"  📦 Flushed [{self.section}|{self.reference}] "
            f"→ {len(chunks)} chunks from pages {self.pages}",
            flush=True,
        )
        return total_chunks

    def _flush_ad(self, total_chunks):
        """AD 2/AD 3: one chunk per subsection (header + values together), each
        with its field term pack. Oversize subsections (e.g. 2.22 flight
        procedures) are sub-split but every piece keeps the header+terms prefix."""
        page0 = self.pages[0] if self.pages else 0
        subs  = ad_subsection_chunks(self.text.strip())
        if not subs:                            # no headers detected -> safe fallback
            prefix = build_prefix(self.part, self.section, self.reference)
            for idx, chunk in enumerate(chunk_text(f"{prefix}\n\n{self.text.strip()}")):
                try:
                    emb = get_embedding(chunk)
                    db_save(self.part, self.section, self.reference, chunk, emb, page0, idx)
                    total_chunks += 1
                    time.sleep(RATE_PAUSE)
                except Exception as e:
                    print(f"  ⚠️  AD flush error: {e}", flush=True)
            return total_chunks
        idx = 0
        for subsec, body in subs:
            if subsec == "2.22":
                continue
            section_label = f"AD {subsec}"                     # e.g. "AD 2.13"
            terms  = ad_enrichment(subsec)
            prefix = build_prefix(self.part, section_label, self.reference)
            header = f"{prefix}\n{terms}\n\n" if terms else f"{prefix}\n\n"
            full   = header + body
            pieces = ([full] if len(full) <= CHUNK_SIZE
                      else [f"{header}{bp}" for bp in chunk_text(body)])
            for piece in pieces:
                try:
                    emb = get_embedding(piece)
                    db_save(self.part, section_label, self.reference, piece, emb, page0, idx)
                    total_chunks += 1
                    idx += 1
                    time.sleep(RATE_PAUSE)
                except Exception as e:
                    print(f"  ⚠️  AD subsection flush error ({section_label}): {e}", flush=True)
        print(
            f"  📦 Flushed AD [{self.reference}] → {len(subs)} subsections, "
            f"{idx} chunks from pages {self.pages}",
            flush=True,
        )
        return total_chunks


# ── Main ──────────────────────────────────────────────────────────────────────

def process():
    print("🚀 Starting AIP Vectorization v2...", flush=True)
    print(f"   PDF  : {PDF_PATH}", flush=True)
    print(f"   Sections in SECTION_TITLES: {len(SECTION_TITLES)}", flush=True)
    print(f"   Accumulate sections: {len(ACCUMULATE_SECTIONS)}", flush=True)

    doc  = fitz.open(PDF_PATH)
    prog = load_progress()
    done = set(prog["done"])
    total_chunks = prog["chunks"]

    chart_pages = load_chart_pages()
    print(f"   Chart/plate pages to EXCLUDE from text index: {len(chart_pages)}", flush=True)
    if not chart_pages:
        print("   ⚠️  aip_charts returned no pages. Run extract_charts.py FIRST so "
              "plate pages can be excluded.", flush=True)
        if os.getenv("ALLOW_NO_CHART_EXCLUSION") != "1":
            print("   Aborting to avoid re-polluting the text index "
                  "(set ALLOW_NO_CHART_EXCLUSION=1 to override).", flush=True)
            return

    current_part      = "GEN"
    current_section   = "GEN 0.1"
    current_reference = "NATIONAL"

    acc = Accumulator()

    def try_flush(np, ns, nr):
        nonlocal total_chunks
        if acc.active() and not acc.matches(np, ns, nr):
            total_chunks = acc.flush(total_chunks)
            acc.reset()

    for page_num in range(len(doc)):
        if page_num in done:
            continue

        # Skip chart PLATE pages — image-only content for aip_charts, not the text
        # index. source_page is 1-indexed; the loop's page_num is 0-indexed.
        if (page_num + 1) in chart_pages:
            done.add(page_num)
            continue

        page = doc[page_num]
        raw  = extract_page_text(page).strip()   # column-aware reading order

        # Second-layer guard: a plate page missed by the aip_charts list (its
        # text layer is chart annotations). Marker-based, safe for numeric tables.
        if looks_like_plate(raw):
            done.add(page_num)
            continue

        if len(raw) < 50:
            done.add(page_num)
            continue

        header_area = raw[:800]
        text_clean  = " ".join(strip_header(raw).split())

        # ── State machine ──────────────────────────────────────────────────────
        new_part      = current_part
        new_reference = current_reference

        if "PART 1" in header_area or "(GEN)" in header_area:
            new_part, new_reference = "GEN", "NATIONAL"
        elif "PART 2" in header_area or "(ENR)" in header_area:
            new_part, new_reference = "ENR", "AIRSPACE"
        elif "PART 3" in header_area or "(AD)" in header_area:
            new_part = "AD"

        m = SECTION_PAT.search(header_area)
        new_section = m.group(0).upper() if m else current_section

        # AD pages: update ICAO reference
        if new_part == "AD":
            m2 = ICAO_PAT.search(header_area)
            if m2:
                new_reference = m2.group(1).upper()
            # Normalize AD 2.x / AD 3.x to a stable base ("AD 2") so ALL of an
            # aerodrome's pages accumulate as one span; subsection granularity is
            # recovered at flush by ad_subsection_chunks(). Without this the state
            # machine would flush on every subsection header and re-fragment.
            base = re.match(r"AD ([23])", new_section)
            if base and new_reference.startswith("DN"):
                new_section = f"AD {base.group(1)}"

        # ENR 6: tag as DNKK for en-route chart retrieval
        if new_part == "ENR" and (
            new_section == "ENR 6"
            or "ENROUTE CHART" in header_area.upper()
            or "EN-ROUTE CHART" in header_area.upper()
        ):
            new_reference = "DNKK"

        if (new_part != current_part
                or new_section != current_section
                or new_reference != current_reference):
            try_flush(new_part, new_section, new_reference)

        current_part      = new_part
        current_section   = new_section
        current_reference = new_reference

        # ── Accumulator for AD 2/AD 3 aerodrome pages (subsection-aware) ───────
        # Whole aerodrome AD span accumulates, then splits by subsection at flush
        # so each field's header+values stay together and become retrievable.
        if (current_part == "AD" and current_section in ("AD 2", "AD 3")
                and (current_reference or "").startswith("DN")):
            if not acc.matches(current_part, current_section, current_reference):
                try_flush(current_part, current_section, current_reference)
                acc.reset(current_part, current_section, current_reference)
            acc.add(text_clean, page_num + 1)
            done.add(page_num)
            continue

        # ── Accumulator for tabular ENR sections ──────────────────────────────
        if current_part == "ENR" and current_section in ACCUMULATE_SECTIONS:
            if not acc.matches(current_part, current_section, current_reference):
                try_flush(current_part, current_section, current_reference)
                acc.reset(current_part, current_section, current_reference)
            enrichment = enr_enrichment(current_section)
            acc.add(enrichment + text_clean, page_num + 1)
            done.add(page_num)
            continue

        # ── Standard embed and store ──────────────────────────────────────────
        prefix    = build_prefix(current_part, current_section, current_reference)
        full_text = f"{prefix}\n\n{text_clean}"
        chunks    = chunk_text(full_text)

        for idx, chunk in enumerate(chunks):
            try:
                emb = get_embedding(chunk)
                db_save(current_part, current_section, current_reference,
                        chunk, emb, page_num + 1, idx)
                total_chunks += 1
                time.sleep(RATE_PAUSE)
            except Exception as e:
                print(f"  ⚠️  Error p{page_num+1} chunk{idx}: {e}", flush=True)

        done.add(page_num)
        prog["done"]   = list(done)
        prog["chunks"] = total_chunks

        if (page_num + 1) % 10 == 0:
            save_progress(prog)
            print(
                f"  ✅ p{page_num+1:4d} | [{current_part}|{current_section}|"
                f"{current_reference}] | chunks:{total_chunks}",
                flush=True,
            )

    total_chunks = acc.flush(total_chunks)
    prog["done"]   = list(done)
    prog["chunks"] = total_chunks
    save_progress(prog)
    print(f"\n🎉 Vectorization complete — {total_chunks} chunks stored.", flush=True)
    print("▶ Now run: python3 extract_charts.py", flush=True)


if __name__ == "__main__":
    print("\n⚠️  Ensure aip_knowledge_base is empty before running.", flush=True)
    print("   Run in Supabase SQL Editor: DELETE FROM aip_knowledge_base;\n", flush=True)
    if input("Confirmed empty? (yes/no): ").strip().lower() == "yes":
        process()
    else:
        print("Aborted. Clear the table first.", flush=True)
