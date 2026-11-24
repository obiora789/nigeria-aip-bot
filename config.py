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
# PINNED TO A DATED SNAPSHOT (audit item, 2026-10-27). The bare alias
# "gpt-4o-mini" lets OpenAI change the model under Vannie with no code change,
# so an answer could not be reproduced or explained later. Every prompt and
# every measurement (run_all_checks.sh, quick_check.py) was made on this
# snapshot. Moving to another model is a deliberate change: change it here,
# then re-run the full checks before deploying.
# OpenAI shuts this snapshot down on 2027-01-14 (announced 2026-10-14;
# recommended replacement gpt-6-luna) — see MODEL_SHUTDOWN below.
#
# MOVED TO gpt-6-luna (2026-11-25). Measured on the same code before the
# switch (full run checks_20261124_1409 against gpt-4o-mini run 2328): stress
# PASS 180 vs 160 of 270, subsection 189 vs 154 of 266, ambiguity mistakes 4 vs
# 9, boundary refusals 0 vs 2, false refusals 1 vs 1, approach routing all
# correct. Live on Render from 2026-11-25 via the environment variables first.
# gpt-6-luna HAS NO DATED SNAPSHOT (OpenAI's model page lists only the alias,
# checked 2026-11-25), so it cannot be pinned: see NO_DATED_SNAPSHOT. Re-run
# the quick check monthly and whenever OpenAI announces a Luna update.
EXTRACTION_MODEL = os.getenv("EXTRACTION_MODEL", "gpt-6-luna")
# Models OpenAI publishes ONLY as an alias. Pin a dated snapshot as soon as one
# is listed; until then this set is the one exception to the pinning rule.
NO_DATED_SNAPSHOT = {"gpt-6-luna"}
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

# Secret pepper for the query log's chat-ID hash. WITHOUT THIS THE HASH IS
# COSMETIC: Telegram chat IDs are sequential integers in a small, enumerable
# space, so anyone who obtains the log can hash every plausible ID and recover
# exactly who asked what — pilots' identities against their queries. A pepper
# the attacker does not have makes that pre-computation impossible.
#
# Set it to a long random value (e.g. `openssl rand -hex 32`) and NEVER rotate
# it casually: hashes are the join key, so a new pepper orphans every existing
# log row from its successors. Rotate only alongside a log prune.
#
# Deliberately not defaulted to a constant — a shared default is the same as no
# pepper at all. Blank means the operator has not set it, and observability
# warns once at import rather than failing the boot (query logging is
# non-essential; the bot must still fly).
CHAT_HASH_PEPPER = os.getenv("CHAT_HASH_PEPPER", "")

# --- Audit trail, retention, privacy (audit items, 2026-10-27) ----------------
# Published OpenAI shutdown dates for the models above. Startup and /health warn
# from MODEL_SHUTDOWN_WARN_DAYS before the date, so the move is planned, not a
# surprise outage. Source: developers.openai.com/api/docs/deprecations.
MODEL_SHUTDOWN = {
    "gpt-4o-mini": "2027-01-14",
    "gpt-4o-mini-2024-07-18": "2027-01-14",
}
MODEL_SHUTDOWN_WARN_DAYS = int(os.getenv("MODEL_SHUTDOWN_WARN_DAYS", "90"))

# Which code answered: Render sets RENDER_GIT_COMMIT on every deploy.
APP_VERSION = (os.getenv("RENDER_GIT_COMMIT") or os.getenv("APP_VERSION") or "")[:12]

# Query-log rows (questions, answers, feedback) are deleted automatically after
# this many days; 0 turns the automatic deletion off. Expired conversation
# context is deleted on the same daily pass.
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "180"))
RETENTION_INTERVAL_SEC = int(os.getenv("RETENTION_INTERVAL_SEC", "86400"))

# Who pilots contact about their data (an email address). Shown in /privacy.
PRIVACY_CONTACT = os.getenv("PRIVACY_CONTACT", "")

# The webhook REFUSES every update when TELEGRAM_WEBHOOK_SECRET is unset.
# Local development only: ALLOW_INSECURE_WEBHOOK=1 lets it through.
ALLOW_INSECURE_WEBHOOK = os.getenv("ALLOW_INSECURE_WEBHOOK", "0") == "1"


def model_shutdown_warnings(today=None) -> list:
    """One line per configured model whose published shutdown is near or past."""
    import datetime as _dt
    today = today or _dt.date.today()
    out = []
    for role, name in (("extraction/judge", EXTRACTION_MODEL),
                       ("synthesis", SYNTHESIS_MODEL)):
        date = MODEL_SHUTDOWN.get(name)
        if not date:
            continue
        days = (_dt.date.fromisoformat(date) - today).days
        if days < 0:
            out.append(f"{role} model {name} was shut down by OpenAI on {date}")
        elif days <= MODEL_SHUTDOWN_WARN_DAYS:
            out.append(f"{role} model {name} is shut down by OpenAI on {date} "
                       f"({days} days) — move to a new model and re-run the full checks")
    return out


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
AIP_PDF = os.getenv("PDF_PATH")
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
# Field-level fact retrieval (aip_facts). One embedding per FIELD instead of
# one per subsection — DNMM's AD 2.22 was 79,871 characters behind a single
# vector, which is measurably why the top chunk was so often the wrong
# section. A fact embeds a sentence close to how a pilot would ask for it.
#
# Facts are shown VERBATIM (responder.facts_reply), never synthesized, so
# this path has no hallucination surface: each line is a stored value with
# its own entity and label.
#
# MIN_SIM is the confidence floor — below it the retriever has no real
# opinion and we fall through to the existing behaviour rather than answer
# from a weak match. MAX is how many facts to show.
FACTS_ENABLED = os.getenv("FACTS_ENABLED", "0") == "1"
FACTS_MIN_SIM = float(os.getenv("FACTS_MIN_SIM", "0.42"))
FACTS_MAX = int(os.getenv("FACTS_MAX", "6"))

SEMANTIC_SUBSECTION_ENABLED = os.getenv("SEMANTIC_SUBSECTION_ENABLED", "0") == "1"
SEMANTIC_SUBSECTION_MIN_SIM = float(os.getenv("SEMANTIC_SUBSECTION_MIN_SIM", "0.35"))
SEMANTIC_SUBSECTION_MARGIN = float(os.getenv("SEMANTIC_SUBSECTION_MARGIN", "0.03"))
SYNTHESIS_MODEL = os.getenv("SYNTHESIS_MODEL", "gpt-6-luna")   # see EXTRACTION_MODEL

# --- Reasoning models (2026-11-07) -------------------------------------------
# gpt-6-luna (OpenAI's replacement for gpt-4o-mini) and other reasoning models
# think before answering unless told not to; the default effort is "medium",
# which adds cost (reasoning tokens bill as output) and delay. They accept
# temperature ONLY with reasoning_effort "none". Vannie's calls are all
# temperature 0 with structured outputs, so the closest like-for-like setting
# is "none". Raise an effort only if measurement shows it pays for itself.
# Extraction and the double-meaning judge share EXTRACTION_REASONING_EFFORT.
REASONING_MODEL_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "none")
EXTRACTION_REASONING_EFFORT = os.getenv("EXTRACTION_REASONING_EFFORT", REASONING_EFFORT)
SYNTHESIS_REASONING_EFFORT = os.getenv("SYNTHESIS_REASONING_EFFORT", REASONING_EFFORT)
for _name, _val in (("REASONING_EFFORT", REASONING_EFFORT),
                    ("EXTRACTION_REASONING_EFFORT", EXTRACTION_REASONING_EFFORT),
                    ("SYNTHESIS_REASONING_EFFORT", SYNTHESIS_REASONING_EFFORT)):
    if _val not in REASONING_EFFORTS:
        raise RuntimeError(f"{_name}={_val!r}: must be one of {', '.join(REASONING_EFFORTS)}")


def is_reasoning_model(model) -> bool:
    return str(model or "").startswith(REASONING_MODEL_PREFIXES)


def reasoning_effort_for(model, role: str):
    """The effort sent for this model and role ('extraction' or 'synthesis'),
    or None for a model without reasoning (gpt-4o-mini)."""
    if not is_reasoning_model(model):
        return None
    return SYNTHESIS_REASONING_EFFORT if role == "synthesis" else EXTRACTION_REASONING_EFFORT


def model_params(model, role: str) -> dict:
    """Sampling parameters for one chat call, so switching model is a settings
    change only. gpt-4o-mini: exactly what Vannie always sent (temperature 0).
    A reasoning model: its effort, plus temperature 0 when the effort is "none"
    (the only case such models accept it). The effort travels in extra_body, so
    any openai SDK version sends it."""
    effort = reasoning_effort_for(model, role)
    if effort is None:
        return {"temperature": 0.0}
    params = {"extra_body": {"reasoning_effort": effort}}
    if effort == "none":
        params["temperature"] = 0.0
    return params
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

# Side-by-side measurement switch for prompts_v2.py. Unset or "v1" keeps the
# prompt above; "v2" selects the restructured prompt (and synthesize.py
# then labels each excerpt with its aerodrome as well as its section).
SYNTHESIS_PROMPT_VERSION = os.getenv("SYNTHESIS_PROMPT", "v1").lower()
if SYNTHESIS_PROMPT_VERSION == "v2":
    from prompts_v2 import SYNTHESIS_SYSTEM_V2 as SYNTHESIS_SYSTEM  # noqa: E402

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
    "Reference aid only (AI assistant) — NOT an operational source. "
    "Verify against the official AIP and current NOTAMs before flight. "
    "NOTAMs supersede the AIP."
)

# S8 — Precision Approach Terrain Charts are safety-critical; never interpret values.
S8_TERRAIN_CAVEAT = (
    "⚠ This chart shows terrain/obstacle data for the precision approach path. "
    "Verify all obstacle clearance altitudes against the published minima in AD 2.22 "
    "and current NOTAMs before commencing the approach."
)

# AI disclosure (audit item, 2026-10-27; EU AI Act Art. 50 from 2026-08-02 for
# EU users): say plainly that this is an AI system and which provider runs it.
AI_NOTICE = (
    "I'm an AI system. OpenAI models read your question and find the right part "
    "of the stored Nigerian AIP; the values I show are taken from that AIP text, "
    "but I can still make mistakes."
)

GREETING = (
    "Hi, I'm Vannie — an AI assistant for the Nigerian AIP. "
    "Ask me about an aerodrome's published frequencies, runway data, procedures, "
    "or charts (e.g. \"Lagos tower frequency\" or \"ILS chart for DNAA runway 04\").\n\n"
    f"{AI_NOTICE}\n\n"
    f"{DISCLAIMER}\n"
    "Send /privacy to see what I store."
)

HELP = (
    "Vannie — Nigerian AIP reference assistant (AI).\n"
    f"Data: Nigeria AIP · {AIRAC_CYCLE}.\n\n"
    f"How I work: {AI_NOTICE}\n\n"
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
    "\nSend /reset any time to clear the aerodrome I am remembering."
    "\n/privacy — what I store about you · /deletemydata — delete it"
)

def privacy_notice() -> str:
    """The /privacy reply. Built at call time so PRIVACY_CONTACT and
    LOG_RETENTION_DAYS always show their current values. Keep in step with
    DATA_PROTECTION.md."""
    keep = (f"deleted automatically after {LOG_RETENTION_DAYS} days"
            if LOG_RETENTION_DAYS > 0 else "kept until deleted by the operator")
    lines = [
        "Privacy — what Vannie stores",
        "",
        "• Your question, my answer, the aerodrome and AIP section used, your "
        "👍/👎, and the time. Your Telegram ID is stored only as a keyed "
        "one-way code, never as the ID itself.",
        "• Why: to answer you, and to find and fix wrong answers (safety review).",
        f"• How long: {keep}. The aerodrome I remember between messages expires "
        f"after {CONTEXT_TTL_MIN} minutes.",
        "• Who processes it: OpenAI (USA) receives the text of your question to "
        "understand it — OpenAI says API data is not used to train its models "
        "and is kept up to 30 days for abuse monitoring. Supabase stores the "
        "log; Render runs the service; Telegram carries the messages.",
        "• Your rights: /deletemydata deletes everything stored against your "
        "Telegram ID now. You can also ask for a copy, a correction, or "
        "complain to the Nigeria Data Protection Commission.",
    ]
    if PRIVACY_CONTACT:
        lines.append(f"• Contact: {PRIVACY_CONTACT}")
    return "\n".join(lines)


OUT_OF_SCOPE = (
    "I'm limited to the published Nigerian AIP. I can't help with live weather, "
    "active NOTAMs, ATC clearances, international airspace, or anything operational "
    "in real time. Please use official real-time sources for those."
)
