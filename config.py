"""
config.py — single source of truth for settings, models, and safety copy.

Required env vars fail fast (KeyError) so the app never boots half-configured.
Run this once to confirm the hard-filter values against your real data:
    select distinct aip_part, reference_tag from aip_knowledge_base;
"""
import os

try:                                  # load .env so ANY entry point (eval_set,
    from dotenv import load_dotenv    # harness, e2e, main) is configured the same
    load_dotenv()                     # way, regardless of how it's launched.
except ImportError:                   # if python-dotenv isn't installed, fall back
    pass                              # to whatever is already in the environment.

# --- OpenAI / models -------------------------------------------------------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
EXTRACTION_MODEL = os.getenv("EXTRACTION_MODEL", "gpt-4o-mini")
# MUST match the model that built your stored vectors (1536-dim).
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

# --- Supabase --------------------------------------------------------------
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# --- Telegram --------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Set this when registering the webhook (Telegram echoes it in a header).
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

# --- AIP currency ----------------------------------------------------------
# The whole corpus is one edition, so currency is an app-level constant.
AIRAC_CYCLE = os.getenv("AIRAC_CYCLE", "AIRAC AMDT 03/2026")

# --- Retrieval -------------------------------------------------------------
SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.40"))
MATCH_LIMIT = int(os.getenv("MATCH_LIMIT", "15"))   # retrieve wider; the grounded
MAX_DISPLAY_CHUNKS = int(os.getenv("MAX_DISPLAY_CHUNKS", "3"))  # step reads them all
MAX_CHARTS = int(os.getenv("MAX_CHARTS", "6"))   # cap (Kano FIR returns ~10 plates)

# --- Grounded synthesis (the role upgrade) ----------------------------------
# Vannie may now SYNTHESIZE and COMPUTE — but only over values found in the
# retrieved AIP excerpts, verified by a deterministic checker that fails safe to
# verbatim display. Set SYNTHESIS_ENABLED=0 to revert to pure verbatim.
SYNTHESIS_ENABLED = os.getenv("SYNTHESIS_ENABLED", "1") == "1"
QUERY_LOG_ENABLED = os.getenv("QUERY_LOG_ENABLED", "1") == "1"
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")   # empty -> /dashboard disabled

# --- Alerting: ping the operator when a credential/infra check degrades --------
ALERT_ENABLED = os.getenv("ALERT_ENABLED", "1") == "1"
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")       # Telegram chat to alert; blank -> off
ALERT_MIN_INTERVAL = int(os.getenv("ALERT_MIN_INTERVAL", "900"))   # anti-spam, seconds
DEEP_CHECK_INTERVAL_SEC = int(os.getenv("DEEP_CHECK_INTERVAL_SEC", "600"))  # 0 -> off

# --- Short-term conversation context (slot-fill + follow-ups) ------------------
CONTEXT_ENABLED = os.getenv("CONTEXT_ENABLED", "1") == "1"
CONTEXT_TTL_MIN = int(os.getenv("CONTEXT_TTL_MIN", "10"))   # context expires fast

# Verbatim approach-procedure text (Holding/Letdown/Missed Approach from AD 2.22).
# OFF by default: it must be validated against the real re-ingested data and a
# sample of aerodromes first (does each section's text match the plate exactly?).
# While off, approach requests get the plate-pointer. The plate always follows
# either way, so this only controls whether the text is shown too.
PROCEDURES_TEXT_ENABLED = os.getenv("PROCEDURES_TEXT_ENABLED", "0") == "1"

# Semantic subsection routing (AD 2.x). When subsection_router's keyword match
# declines, fall back to picking the subsection the RETRIEVER ranked highest,
# and answer from that one section instead of synthesizing over all retrieved
# chunks. Catches phrasings no keyword list anticipates ("what services are
# available at Enugu" -> AD 2.4) while keeping synthesis scoped to a single
# subsection, so a fact cannot be drawn from one section and cited to another.
#
# OFF by default: semantic matching can be confidently wrong where keywords
# simply stay silent, so it should be measured against the eval set before
# being switched on. The two thresholds are the safety valves — a weak top
# match or a near-tie between sections both decline, leaving the pre-existing
# behaviour untouched.
SEMANTIC_SUBSECTION_ENABLED = os.getenv("SEMANTIC_SUBSECTION_ENABLED", "0") == "1"
SEMANTIC_SUBSECTION_MIN_SIM = float(os.getenv("SEMANTIC_SUBSECTION_MIN_SIM", "0.35"))
SEMANTIC_SUBSECTION_MARGIN = float(os.getenv("SEMANTIC_SUBSECTION_MARGIN", "0.03"))
SYNTHESIS_MODEL = os.getenv("SYNTHESIS_MODEL", "gpt-4o-mini")
SYNTHESIS_CONTEXT_CHUNKS = int(os.getenv("SYNTHESIS_CONTEXT_CHUNKS", "15"))

SYNTHESIS_SYSTEM = (
    "You answer questions about the 2026 Nigerian AIP for pilots, using ONLY the "
    "AIP excerpts given in the user message. You are a safety reference aid, not an "
    "operational source.\n\n"
    "ABSOLUTE RULES:\n"
    "1. Use ONLY facts that appear in the excerpts. Never use outside knowledge, "
    "training data, or assumptions. If the excerpts do not fully contain the answer, "
    "set answerable=false and leave answer empty.\n"
    "2. Quote every value EXACTLY as written — same digits, same units. Never round, "
    "convert, or reformat a number. When a value is given in BOTH metric and imperial "
    "(e.g. '342 m / 1122 ft'), include BOTH.\n"
    "3. You MAY compare values, pick the largest/smallest, count, and do arithmetic, "
    "but ONLY with numbers copied from the excerpts. List every source value in "
    "facts_used and show EVERY arithmetic step in 'computation' as 'A op B = C' "
    "(if you state two differences, e.g. longer AND wider, show BOTH steps separated "
    "by ';'). Any number you state that isn't copied from an excerpt must appear as "
    "the result of a step you show.\n"
    "4. NEVER state a number, frequency, altitude, distance, bearing, or identifier "
    "that is not present in the excerpts. The only exception is a value that is the "
    "result of arithmetic you show in 'computation'.\n"
    "5. If the excerpts are ambiguous, contradictory, or only partially answer the "
    "question, set answerable=false rather than guessing.\n"
    "6. The excerpts may contain several aerodromes' data and unrelated tables — use "
    "only the rows that match what was asked (the right aerodrome, runway, field).\n"
    "7. Give the complete published answer: for a runway's dimensions/characteristics "
    "include length x width, surface, and strength (PCN/PCR) when present; for "
    "elevation include both feet and metres. Don't omit a value the excerpt provides.\n"
    "8. A runway has TWO ends sharing one strip (e.g. '06/24' is ONE physical runway "
    "with ends 06 and 24). When asked HOW MANY runways, count physical runways "
    "(reciprocal pairs), not the individual end designators. When asked to LIST "
    "runways, list the paired designators (e.g. 'RWY 06/24, RWY 05/23').\n"
    "9. Be concise and factual. State the AIP fact only — no advice, no reassurance, "
    "no 'you should'. Do not add a disclaimer (the system adds one)."
)

# Hard-filter fallback sets (used to neutralise LLM part/reference misclassification).
AIP_PARTS = ["AD", "ENR", "GEN"]
# Real non-ICAO reference_tag values in aip_knowledge_base. Confirm the full set:
#   select reference_tag, count(*) from aip_knowledge_base
#   where reference_tag !~ '^DN[A-Z]{2}$' group by reference_tag order by 2 desc;
NATIONAL_REFERENCE_TAGS = [
    t.strip() for t in os.getenv("NATIONAL_REFERENCE_TAGS", "NATIONAL,AIRSPACE,DNKK").split(",") if t.strip()
]

# --- Abuse / throttle ------------------------------------------------------
PER_CHAT_COOLDOWN_SECONDS = float(os.getenv("PER_CHAT_COOLDOWN_SECONDS", "1.5"))
DEDUP_CACHE_SIZE = int(os.getenv("DEDUP_CACHE_SIZE", "2048"))

# --- Distributed dedup/throttle (optional) ------------------------------------
# Set REDIS_URL to share dedup + throttle state across restarts and instances.
# Unset -> in-memory fallback (single-instance, wiped on restart) — unchanged.
REDIS_URL = os.getenv("REDIS_URL", "")
DEDUP_TTL_SEC = int(os.getenv("DEDUP_TTL_SEC", "3600"))   # how long an update_id is remembered

# --- Safety copy -----------------------------------------------------------
DISCLAIMER = (
    "Reference aid only — NOT an operational source. "
    "Verify against the official AIP and current NOTAMs before flight. "
    "NOTAMs supersede the AIP."
)

# S8 — Precision Approach Terrain Charts are safety-critical; never interpret values.
S8_TERRAIN_CAVEAT = (
    "⚠ This chart shows terrain/obstacle data for the precision approach path. "
    "Verify all obstacle clearance altitudes against the published minima in AD 2.22 "
    "and current NOTAMs before commencing the approach."
)

GREETING = (
    "Hi, I'm Vannie — a reference assistant for the Nigerian AIP. "
    "Ask me about an aerodrome's published frequencies, runway data, procedures, "
    "or charts (e.g. \"Lagos tower frequency\" or \"ILS chart for DNAA runway 04\").\n\n"
    f"{DISCLAIMER}"
)

HELP = (
    "Vannie — Nigerian AIP reference assistant.\n"
    f"Data: Nigeria AIP · {AIRAC_CYCLE}.\n\n"
    "What I can look up, per aerodrome:\n"
    "• Frequencies — tower, approach, ground, ATIS\n"
    "• Runway data — dimensions, declared distances (TORA/TODA/ASDA/LDA), PCN\n"
    "• Elevation, reference temperature, transition altitude\n"
    "• Navaids — VOR/DME/ILS identifiers and frequencies\n"
    "• Approach procedures — holding, letdown, missed approach\n"
    "• Charts/plates — ILS, VOR, RNAV, SID, STAR, aerodrome, parking\n"
    "• ICAO ↔ city mapping, and where a topic sits in the AIP\n\n"
    "Tips:\n"
    "• Name the aerodrome (city or ICAO, e.g. Lagos or DNMM).\n"
    "• Add a runway/procedure for charts: \"VOR approach for Lagos RWY 18L\".\n"
    "• Tap 👍/👎 under an answer — 👎 flags it for review.\n\n"
    "What I don't do: live weather/METAR/TAF values, NOTAMs, flight planning, or "
    "anything outside the published Nigerian AIP.\n\n"
    f"{DISCLAIMER}"
)

OUT_OF_SCOPE = (
    "I'm limited to the published Nigerian AIP. I can't help with live weather, "
    "active NOTAMs, ATC clearances, international airspace, or anything operational "
    "in real time. Please use official real-time sources for those."
)
