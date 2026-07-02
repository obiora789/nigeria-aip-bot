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
    "8. Be concise and factual. State the AIP fact only — no advice, no reassurance, "
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

OUT_OF_SCOPE = (
    "I'm limited to the published Nigerian AIP. I can't help with live weather, "
    "active NOTAMs, ATC clearances, international airspace, or anything operational "
    "in real time. Please use official real-time sources for those."
)
