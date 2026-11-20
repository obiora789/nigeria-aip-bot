"""
disambiguate.py — a question with two real readings: answer the usual one,
and offer the other with ONE button under the answer.

DESIGN (2026-10-26, replaces "ask first")

Measured 2026-10-26 (checks_20261026_2250, checks_quick_20261026_2324): an
open-ended judge ("list every meaning, then decide whether to ask") asked on
almost everything that contained a listed word. 'approach plate for Sokoto',
'Abuja VOR approach', 'RNAV approach for X', 'ILS minima for X', 'take-off
minima', 'landing fee' all got buttons; approach routing fell from 658/720
to 374/720. A small model can nearly always imagine a second meaning, and
once it has written two readings it argues itself into asking.

So the model is no longer asked to imagine meanings. It answers three narrow
questions about the wording (schemas.AmbiguityCheck):
  1. topic — what the question asks about, COPIED from the question without
     the place and question words: 'approach', 'approach frequency',
     'landing fee', 'clearance priorities'. Written FIRST, so the evidence
     comes before the label.
  2. family — which of SIX known bare questions this is, or "none". Only a
     ONE-WORD topic that is one of the six can be a double meaning.
  3. place — the aerodrome as typed.
Every decision is still the model's; nothing here reads the pilot's words.
decide() only checks the model's answers agree with each other (a family is
named AND the model's own topic is a single word).

Second measurement, same day (checks_quick_20261026_2352), with a
"narrowing words" field instead of topic: 80/80 bare questions, every
approach form right, but 'VFR clearance', 'landing fee', 'clearance
priorities', 'taxiway info' and even 'dimensions' / 'fuelling' were labelled
double meanings with no narrowing words. The model only listed words it had
been shown as examples. Copying the whole topic phrase avoids that.

The six families, their default reading and the one alternative are fixed
below (FAMILIES). The model never writes button text or rewritten questions,
so a button can never offer something odd ("Ogun ILS unit", Sokoto for Soku).

PRESENTATION (Obiora's choice, option B): never make the pilot wait. Vannie
answers the default reading at once, through the normal pipeline, and puts
one button under the answer: "Did you mean the approach plate?". A tap
re-runs the alternative question. The button is left off when the AIP has
nothing for the alternative at that aerodrome (alternative_exists).

New families are added here deliberately, from evidence (logs, stress runs,
which button pilots tap) — never guessed by the model at run time. Anything
outside the six is answered exactly as before this feature.

Taps are logged: the answer row's query is "[default:<family>] <question>",
a tap's is "[alt:<family>] <question>". If pilots tap one family's button
most of the time, its default is the wrong way round — swap it here.
"""
import logging

log = logging.getLogger("vannie.disambiguate")

# ---------------------------------------------------------------- families
# default / alt: (short name, question rewritten to ONE meaning; {p} = place)
# button:        text of the single button under the default answer (<= 40)
# alt_section:   where the alternative lives, used by alternative_exists()
FAMILIES = {
    "approach": {   # default per Obiora 2026-10-21: "Let sokoto approach defer to the ATC unit"
        "default": ("Approach unit (ATC)", "What is the frequency of {p} Approach?"),
        "alt": ("Approach plate", "Show me the instrument approach plate for {p}"),
        "button": "Did you mean the approach plate?",
        "alt_section": "approach_plate",
    },
    "minima": {
        "default": ("Approach minima", "What are the approach minima for {p}?"),
        "alt": ("Take-off minima", "What are the take-off minima for {p}?"),
        "button": "Did you mean take-off minima?",
        "alt_section": None,                 # no reliable stored signal: always offer
    },
    "clearance": {
        "default": ("ATC clearance, VFR",
                    "What are the ATC clearance requirements for VFR flights at {p}?"),
        "alt": ("Movement-area clearing",
                "What are the clearing equipment and clearance priorities for the "
                "movement area at {p} (AD 2.7)?"),
        "button": "Did you mean movement-area clearing?",
        "alt_section": "AD 2.7",
    },
    "lighting": {
        "default": ("Runway and approach lighting",
                    "What is the runway and approach lighting at {p} (AD 2.14)?"),
        "alt": ("Other lighting",
                "What is the apron, taxiway, beacon and other lighting at {p} (AD 2.15)?"),
        "button": "Did you mean apron/taxiway lighting?",
        "alt_section": "AD 2.15",
    },
    "taxiing": {
        "default": ("Taxiing rules",
                    "What are the taxiing rules and limitations at {p} (AD 2.20)?"),
        "alt": ("Taxiway data",
                "What are the taxiway width, surface and strength at {p} (AD 2.8)?"),
        "button": "Did you mean taxiway data (AD 2.8)?",
        "alt_section": "AD 2.8",
    },
    "charges": {
        "default": ("Aerodrome charges",
                    "What are the aerodrome charges, such as landing and parking, at {p}?"),
        "alt": ("Air navigation charges", "What are the air navigation charges (GEN 4.2)?"),
        "button": "Did you mean air navigation charges?",
        "alt_section": "GEN 4.2",
    },
}
_EMPTY = {"", "none", "n/a", "na", "nil", "-", "null", "no"}
CALLBACK_MAX = 64        # Telegram's callback_data limit, in bytes


JUDGE_SYSTEM = """\
You label ONE question sent to Vannie, a reference assistant for the Nigerian \
AIP used by pilots and air traffic controllers. You do not answer it.

Six short questions, typed BARE, can mean two different parts of the AIP. Your \
only job is to say whether this question is one of those six in its bare form. \
Almost every question is not, and the answer is then family "none".

Step 1 — topic. Copy what the question asks about, exactly as typed, leaving \
out only the place name and question words (what, is, are, show, me, tell, \
give, the, at, for, in, into, of, on, please). Keep every other word. \
'Kano approach frequency' -> 'approach frequency'; 'landing fee at Kano' -> \
'landing fee'; 'what is the runway lighting for Kano' -> 'runway lighting'.

Step 2 — family. Only a topic of ONE word, and that word one of the six \
below, can be anything but "none". A topic of two or more words is always \
"none": the extra word already says which part of the AIP is wanted.
- "approach": topic 'approach', used like the name of a unit after the \
place: 'Kano approach', 'what is Kano approach', 'show me Kano approach'. \
But 'the approach in/into/at Kano' is about the procedure: "none".
- "minima": topic 'minima'.
- "clearance": topic 'clearance'.
- "lighting": topic 'lighting', 'lights' or 'lgt'.
- "taxiing": topic 'taxiing' or 'taxi'.
- "charges": topic 'charges' or 'fees'.
Any other one-word topic is "none", even a related one: 'dimensions', \
'fuel', 'obstacles', 'frequencies', 'taxiways', 'limits' are all "none". So \
is a question with two topics, a follow-up such as 'and the TORA?', a reply \
that is only a place name, and a greeting.

Examples:
'Lagos approach' -> topic "approach", family approach, place Lagos
'Lagos approach frequency' -> topic "approach frequency", family none
'Jos VOR approach' -> topic "VOR approach", family none
'approach plate for Enugu' -> topic "approach plate", family none
'What is the approach into Ilorin' -> topic "approach", family none (procedure)
'ILS minima for Kano' -> topic "ILS minima", family none
'minima at Yola' -> topic "minima", family minima, place Yola
'take-off minima at Yola' -> topic "take-off minima", family none
'landing fee at Benin' -> topic "landing fee", family none
'charges at Benin' -> topic "charges", family charges, place Benin
'runway lighting at Ilorin' -> topic "runway lighting", family none
'lights at Ilorin' -> topic "lights", family lighting, place Ilorin
'Kano fuel' -> topic "fuel", family none

Step 3 — place: the aerodrome or city exactly as typed, or an empty string.\
"""


# ----------------------------------------------------------------- judging
def judge(question: str):
    """The model's AmbiguityCheck for one question, or None on any failure.
    A failure never blocks or changes an answer: no check, no button."""
    import agent                                   # lazy: agent owns the client
    import config
    from retry import retry_call
    from schemas import AmbiguityCheck
    try:
        r = retry_call(
            agent.client.beta.chat.completions.parse,
            model=config.EXTRACTION_MODEL,
            messages=[{"role": "system", "content": JUDGE_SYSTEM},
                      {"role": "user", "content": question[:2000]}],
            response_format=AmbiguityCheck,
            **config.model_params(config.EXTRACTION_MODEL, "extraction"),
        )
        return r.choices[0].message.parsed
    except Exception:                              # noqa: BLE001
        log.exception("ambiguity check failed")
        return None


def _blank(s) -> bool:
    return (s or "").strip().strip("'\".").lower() in _EMPTY


def decide(check):
    """(family, place) when the question is one of the six bare questions,
    else None. The model's answers must agree with each other: a family is
    named AND its own topic is a single word (a longer topic, such as
    'landing fee' or 'clearance priorities', already says which part is
    wanted). A place is required — the rewritten questions need it."""
    if check is None:
        return None
    family = (getattr(check, "family", None) or "none").strip().lower()
    if family not in FAMILIES:
        return None
    place = " ".join((getattr(check, "place", None) or "").split())
    if _blank(place):
        return None
    # The model sometimes leaves its own place in its topic ('Soku approach'
    # for 'What is Soku approach', measured 2026-10-26). Taking the model's
    # place back out of the model's topic is a consistency step on its own
    # answer, not a reading of the pilot's words.
    words = (getattr(check, "topic", None) or "").strip().strip("'\".?!").split()
    pw = place.lower().split()
    for i in range(len(words) - len(pw) + 1):
        if [w.lower().strip("'\".?!,") for w in words[i:i + len(pw)]] == pw:
            words = words[:i] + words[i + len(pw):]
            break
    if len(words) != 1:
        return None
    return family, place


def pick(question: str):
    """judge + decide: (family, place) or None. Used by main.process()."""
    return decide(judge(question))


def readings(family: str, place: str):
    """[{label, question}] — the default reading first, then the alternative."""
    f = FAMILIES[family]
    return [{"label": f[k][0], "question": f[k][1].format(p=place)}
            for k in ("default", "alt")]


def ask(question: str):
    """The readings for a double-meaning question, or None.

    Kept for check_ambiguity.py / check_approach_routing.py / quick_check.py:
    truthy means "Vannie treats this as a double meaning" (answers the
    default and offers the alternative)."""
    p = pick(question)
    return readings(*p) if p else None


def default_question(family: str, place: str) -> str:
    return FAMILIES[family]["default"][1].format(p=place)


def alt_question(family: str, place: str) -> str:
    return FAMILIES[family]["alt"][1].format(p=place)


# ------------------------------------------------------------- heliports
# Obiora, 2026-10-27: at a heliport "Soku approach" is not a choice between an
# Approach unit and a plate — answer it as typed. A heliport is recognised
# from the stored AIP itself: it has NO AD 2.7, AD 2.15 or AD 2.8 text (Soku,
# DNSK, measured 2026-10-26). ad216_extractor.py names the dedicated
# heliports as DNGB, DNPS, DNSK and DNWI; quick_check.py lists which of the 40
# this rule picks, so the two can be compared.
HELIPORT_EMPTY_SECTIONS = ("AD 2.7", "AD 2.15", "AD 2.8")
NOT_AT_HELIPORTS = {"approach"}
_heliport_cache: dict = {}


def is_heliport(icao) -> bool:
    """True only when every one of HELIPORT_EMPTY_SECTIONS is stored empty.
    No ICAO or a database error -> False (treated as an aerodrome).

    Only "not a heliport" is cached: finding text proves the data was there.
    "Heliport" is re-read each time (three small reads, bare approach
    questions only), so an outage that returned empty results could never
    turn an aerodrome into a heliport for the life of the server."""
    if not icao:
        return False
    if icao in _heliport_cache:
        return False
    try:
        from database import get_subsection_text
        empty = not any((get_subsection_text(icao, sec) or "").strip()
                        for sec in HELIPORT_EMPTY_SECTIONS)
    except Exception:                              # noqa: BLE001
        log.exception("is_heliport failed (%s)", icao)
        return False
    if not empty:
        _heliport_cache[icao] = False
    return empty


def applies_here(family: str, icao) -> bool:
    """Should this double meaning be handled at this aerodrome at all?
    False -> main.process answers the pilot's question exactly as typed."""
    if family in NOT_AT_HELIPORTS and is_heliport(icao):
        return False
    return True


# ------------------------------------------- is the alternative in the AIP?
def alternative_exists(family: str, icao) -> bool:
    """False only when the AIP plainly has nothing for the alternative at this
    aerodrome. Anything uncertain — no ICAO, a database error, no reliable
    signal — returns True: a button that leads to "not published" is a small
    cost, a missing button hides an answer."""
    sec = FAMILIES.get(family, {}).get("alt_section")
    if not sec:
        return True
    try:
        from database import get_charts, get_subsection_text
        if sec.startswith("GEN"):
            return bool((get_subsection_text("NATIONAL", sec) or "").strip())
        if not icao:
            return True
        if sec == "approach_plate":
            # Not the parsed procedure headers: check_procedures_all reports
            # "no instrument-approach headers" for aerodromes that do have
            # approaches (a parsing gap). Any chart, or any AD 2.22 text, is
            # enough to offer the plate.
            if get_charts(icao, "", ""):
                return True
            return bool((get_subsection_text(icao, "AD 2.22") or "").strip())
        return bool((get_subsection_text(icao, sec) or "").strip())
    except Exception:                              # noqa: BLE001
        log.exception("alternative_exists failed (%s, %s)", family, icao)
        return True


def alt_button(family: str, place: str, icao=None):
    """The single button for under the default answer, or None.

    The callback carries the family and the place, so the tap needs no stored
    state: it cannot expire, and a later question cannot overwrite it."""
    f = FAMILIES.get(family)
    if not f:
        return None
    for where in (place, icao):
        if not where:
            continue
        data = f"alt:{family}:{where}"
        if len(data.encode("utf-8")) <= CALLBACK_MAX:
            return {"text": f["button"], "callback_data": data}
    return None


def from_callback(data: str):
    """'alt:<family>:<place>' -> (family, alternative question), or None."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "alt" or parts[1] not in FAMILIES or not parts[2].strip():
        return None
    return parts[1], alt_question(parts[1], parts[2].strip())


# ------------------------------------------------ old "ask first" buttons
# Messages sent before 2026-10-26 can still carry "amb:<i>:<qid>" buttons.
# main.handle_feedback keeps answering those taps from the stored record.
KIND = "amb_clar"


def chosen_question(pending: dict, index: str, qid: str):
    """The rewritten question for an old ask-first tap, or None if stale."""
    if not pending or pending.get("kind") != KIND or pending.get("qid") != qid:
        return None
    try:
        return (pending.get("options") or [])[int(index)]["question"]
    except (ValueError, IndexError, KeyError, TypeError):
        return None


def save_pending(chat_id, query: str, opts, qid: str) -> None:   # pragma: no cover
    """Unused since 2026-10-26; kept so an old import cannot break."""
    import memory
    memory._write(chat_id, last_icao=None, last_query=query,
                  pending={"kind": KIND, "qid": qid, "query": query, "options": opts})
