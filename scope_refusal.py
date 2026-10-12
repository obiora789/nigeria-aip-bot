"""
scope_refusal.py — says WHY a question is out of scope, and where the answer is.

THE PROBLEM THIS REPLACES
-------------------------
config.OUT_OF_SCOPE is one static string, so every refusal is identical:

    "I'm limited to the published Nigerian AIP. I can't help with live weather,
     active NOTAMs, ATC clearances, international airspace, or anything
     operational in real time. Please use official real-time sources for those."

Confirmed live, a pilot asking "Is runway 22 open right now?" got exactly that.
Four of its five clauses are about things he did not ask. It never names the
one source that answers him — a NOTAM — and it never notes that "runway 22"
does not say WHICH aerodrome, so he cannot even ask again correctly.

A refusal that lists capabilities teaches nothing. A refusal that names the
authority teaches the pilot where to go, which is the only useful thing an
out-of-scope reply can do.

NO PATTERN MATCHING. THE CLASSIFIER ALREADY KNOWS.
--------------------------------------------------
An earlier version of this module matched the raw text against regexes for
"closed", "METAR", "clearance" and so on. That was the wrong instrument, for
the reason this project keeps rediscovering: a keyword list is a SHAPE test,
and shape tests are unbounded — a pilot can always phrase around one. "Is 22
usable?", "any restrictions on 22 today?", "can I plan on 22 tonight?" all ask
the same thing and share no keyword.

The extraction LLM has already read the question and already decided to refuse
it. Asking it to also report WHY, as one more field on the same structured
output, costs nothing extra: no second call, no second taxonomy, and the reason
can never contradict the refusal because the same call produces both. This
module is then a pure lookup from that field to the sentence a pilot sees.

The runway and aerodrome come from the same extraction (`ex.runway`,
`ex.aerodrome_name`, `ex.icao_code`), so there is no text parsing here at all.

THE TEST IS PERMANENCE
----------------------
The AIP publishes LASTING information — what is true until the next amendment.
Anything that can change between amendments is, by definition, not in it:

    a runway's dimensions, bearing, declared distances   AIP, AD 2.12/2.13
    whether that runway is open this afternoon           NOTAM

That distinction is the whole reason the reasons below are shaped as they are.
Every one of them is a kind of TRANSIENCE (or a price, or another State), and
each names the authority that actually publishes that kind of thing.

WHAT THIS MUST NOT DO
---------------------
Never imply operational currency. Where the AIP does publish something adjacent
— a runway's dimensions when asked whether it is open — the offer is a SEPARATE
sentence, explicitly labelled published, and always after the refusal. Never
blended into one paragraph that could read as an answer to "is it open". The
AIP is not a NOTAM and must never be offered as a substitute for one.

FAILURE DIRECTION
-----------------
This module is reached only after extract_query_parameters() has already
refused. It cannot turn a refusal into an answer or an answer into a refusal,
so measure_intent.py's false-refusal and false-accept rates are untouched by
anything here. If the model reports a reason that does not fit, the cost is a
slightly-off sentence in a reply that was going to refuse anyway — never a
wrong value. An unrecognised or missing reason falls back to the shipped
string, because naming no reason beats naming the wrong one: a confidently
wrong explanation sends a pilot to the wrong authority.
"""
import config

# reason -> (what to say, whether published runway data is a sensible follow-up)
#
# The second element is NOT "do we have data" — it is "would offering it here
# be honest". Only transient-status questions have a published counterpart worth
# naming, and even then only when a runway was actually named. There is nothing
# to offer alongside a weather question: the AIP carries no wind, visibility or
# QNH values at all.
_REASONS = {
    # A state that changes from day to day: closures, works in progress,
    # serviceability, restrictions in force at the moment. NOT the runway in
    # use — AD 2.22.2 publishes a runway-in-use statement for every aerodrome,
    # so that question is answered from the AIP, never refused here.
    "transient_status": (
        "I can't tell you that — it's the kind of thing that changes between "
        "AIP amendments, so it comes from NOTAMs, ATIS or ATC on the day, not "
        "from the AIP. Check the current NOTAMs or your pre-flight information "
        "bulletin, or ask ATC.",
        True,
    ),
    "current_weather": (
        "That's a current observation. The AIP carries no weather values at "
        "all — the METAR or TAF from the meteorological office is what you "
        "want.",
        False,
    ),
    "atc_clearance": (
        "That's an ATC clearance — only the controller on frequency can issue "
        "or confirm one. The AIP never carries clearances.",
        False,
    ),
    "planning_or_flow": (
        "That's flight planning and flow, which the AIP doesn't publish. Your "
        "operations department or the ATS reporting office handles it.",
        False,
    ),
    # Narrowed: published GEN 4 charges are now ANSWERED (charges.py), so this
    # reason only ever means a price the AIP does not publish. The old copy
    # ("I don't hold charges or fees") became false the moment GEN 4 was wired.
    "charges": (
        "That's a price the AIP doesn't publish. GEN 4 carries aerodrome and "
        "air navigation charges — landing, parking, passenger service, "
        "en-route — but not fuel prices or handling quotes. The aerodrome "
        "operator or your handling agent can quote you.",
        False,
    ),
    "outside_nigeria": (
        "That's outside Nigeria. I only hold the Nigerian AIP — the answer is "
        "in that State's own AIP.",
        False,
    ),
}


def _runway_offer(ex) -> str:
    """The published-data follow-up, or '' when there is nothing honest to say.

    Two separate facts, kept in separate sentences on purpose: the question may
    be under-specified, AND the published data is a different thing from the
    status that was asked for."""
    rwy = (getattr(ex, "runway", None) or "").strip().upper()
    if not rwy:
        # Regression guard: an earlier version volunteered runway data for any
        # transient question, including "are there NOTAMs for Abuja?" — which
        # answers something nobody asked, the exact failure of the static
        # string this module replaces.
        return ""

    named = bool(getattr(ex, "icao_code", None) or getattr(ex, "aerodrome_name", None))
    if not named:
        # "runway 22" is ambiguous across every aerodrome that publishes one.
        # The old reply never said so, leaving the pilot unable to ask again
        # correctly — a refusal that hides WHY it cannot answer.
        return (f"Also worth saying: \"RWY {rwy}\" doesn't tell me which "
                f"aerodrome. Name one and I can give you what the AIP "
                f"publishes for that runway — dimensions, bearing, declared "
                f"distances. That's the published data, not its current "
                f"status.")
    return (f"I can give you what the AIP publishes for RWY {rwy} — "
            f"dimensions, bearing, declared distances — if that's useful. "
            f"Published data, not its current status.")


def out_of_scope(ex=None) -> str:
    """The refusal a pilot should see for THIS question.

    `ex` is the AIPQueryExtraction the caller already has. Nothing is re-parsed
    from the raw text.

    getattr with a default throughout, so this keeps working against an
    extraction object that predates the out_of_scope_reason field — a schema
    change and a deploy do not land at the same instant, and the degraded
    behaviour is exactly today's behaviour rather than a crash."""
    reason = (getattr(ex, "out_of_scope_reason", None) or "").strip()
    entry = _REASONS.get(reason)
    if not entry:
        return config.OUT_OF_SCOPE

    body, may_offer = entry
    parts = [body]
    if may_offer:
        offer = _runway_offer(ex)
        if offer:
            parts.append(offer)
    parts.append(config.DISCLAIMER)
    return "\n\n".join(parts)
