"""
schemas.py — strict structured-output schema for the extraction LLM.

Key change from the original: the model NO LONGER maps city names to ICAO codes.
That mapping was an unguarded hallucination path (wrong code -> wrong airport's
real data shown as authoritative). The model now only reports the raw aerodrome
name; deterministic resolution to an ICAO happens in resolver.py against your own
database.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class GroundedFact(BaseModel):
    """A single AIP value the synthesized answer relies on, quoted verbatim.

    source_excerpt is REQUIRED and is the load-bearing field for the fix to the
    multi-entity misattribution class: it pins each fact to the ONE excerpt (by
    its "--- Excerpt N ---" number) it was actually copied from, so the
    deterministic verifier can check the value against THAT excerpt specifically
    rather than a flattened blob of every retrieved chunk. Without this, a real
    value from Excerpt 3 verifies successfully while the reply cites Excerpt 1's
    section — the exact failure confirmed on a DNMM VFR-restrictions query, whose
    answer was attributed to AD 2.20 when the governing text was actually AD
    2.22.5.1. It also applies to prose facts with no numbers at all (a stated
    rule/restriction), which the old numbers-only verifier could not check at
    all — that gap let a fabricated-or-cross-cited prose claim through with zero
    checks, since a claim with no digits triggered no verification step.
    """
    value: str = Field(description="A value copied EXACTLY from the AIP excerpts, e.g. '3610 m'")
    what: str = Field(description="What this value is, e.g. 'RWY 04 TORA'")
    source_excerpt: int = Field(
        description="The excerpt number (the N in '--- Excerpt N [...] ---') "
                    "this value was copied from. Every fact — numeric or a "
                    "quoted rule/restriction — needs exactly one.")


class GroundedAnswer(BaseModel):
    """Output of the grounded-synthesis step. The LLM may compute/compare, but
    only over values present in the excerpts; a deterministic verifier then
    checks every asserted number against the source before anything is shown."""
    answerable: bool = Field(
        description="True ONLY if the answer is fully supported by the excerpts")
    answer: str = Field(
        default="", description="Concise factual answer using only the excerpts")
    facts_used: List[GroundedFact] = Field(
        default_factory=list,
        description="Every AIP value the answer relies on, quoted exactly")
    computation: str = Field(
        default="", description="Arithmetic shown as 'A op B = C', else empty")


class AIPQueryExtraction(BaseModel):
    """Forces strict JSON. The model extracts parameters only — it does not answer."""

    intent: Literal[
        "frequency_retrieval",
        "chart_retrieval",
        "runway_data",
        "aerodrome_fact",       # AD: elevation, ref temp, taxiways, declared distances,
                                #     RFFS, fuel, facilities, hours, removal of disabled acft
        "procedure_lookup",
        "icao_lookup",          # ICAO code <-> city/airport name mapping
        "airspace_lookup",      # ENR: FIR/TMA/CTR limits, airways, restricted areas, waypoints
        "national_lookup",      # GEN: charges, MET policy, SAR, AIS, abbreviations
        "general_greeting",
        "out_of_scope",
    ] = Field(description="Classify what the pilot is asking for.")

    # SET FOR EVERY QUESTION, NOT ONLY REFUSALS. This is the field the live-
    # query gate in agent._backstop() reads, after every correction has run.
    #
    # It asks about the ANSWER, not the wording. The AIP publishes facts of
    # lasting character; a word like "now" or "current" in the question does
    # not change a frequency, a TA or the AD 2.22.2 runway-in-use statement.
    # The earlier name, time_bound, invited exactly that word-level reading
    # and the model refused published data because of it.
    #
    # Optional[bool] with a None default, not bool=False: identical in shape to
    # out_of_scope_reason below, which is proven to work with the structured-
    # output call. None and False are both treated as "not live".
    needs_live_source: Optional[bool] = Field(
        None,
        description=(
            "Answer for EVERY question. Can the answer only come from a live "
            "source — NOTAM, ATIS, METAR/TAF report or a controller — because "
            "it changes from day to day and the AIP cannot hold it?\n"
            "TRUE only for: whether a runway, taxiway, apron, aerodrome, navaid "
            "or facility is open, closed, serviceable, usable, restricted or "
            "under works at the moment; which NOTAMs are active; an observed or "
            "forecast weather value (METAR, SPECI, TAF report, the wind, "
            "visibility or QNH right now); a clearance or instruction for a "
            "particular flight; a slot, delay or flight status.\n"
            "FALSE for anything the AIP publishes, however it is worded. The "
            "AIP holds information of lasting character, and words such as "
            "now, current, currently, today, tonight, at the moment, latest or "
            "live do NOT make a published fact live: a frequency, call sign, "
            "ident, hours of service, transition altitude, runway dimensions, "
            "declared distances, elevation, RFFS category, a procedure, a rule "
            "or a charge is the same until the next amendment.\n"
            "The runway in use is FALSE: every aerodrome's AD 2.22.2 publishes "
            "a runway-in-use statement, and that statement is the answer.\n"
            "TRUE needs the question to ask about the PRESENT STATE of "
            "something — is it open, closed, working, usable, available; can I "
            "use it at a given time; what is it reading. A question that only "
            "names a topic or a published field asks for the AIP entry and is "
            "FALSE: 'fuel spillage at Yola' (the published spillage-cleaning "
            "service), 'removal of disabled aircraft at Kano', 'takeoff and "
            "landing minima'.\n"
            "Examples, FALSE: 'what is Lagos tower frequency now', 'current "
            "ATIS frequency at Kano', 'which runway is in use at Lagos today', "
            "'Lagos tower hours today', 'current transition altitude at Abuja', "
            "'the current landing fee', 'which AIRAC amendment is this AIP "
            "on', 'TAF validity at Lagos', 'ATC clearance requirements for VFR "
            "flights at Lagos', 'fuel spillage at Yola'.\n"
            "Examples, TRUE: 'is runway 18R open right now', 'can I use 18R "
            "at Lagos tonight', 'is Lagos tower open now', 'any NOTAMs for "
            "Kano', 'current METAR for Lagos', 'the wind at Abuja', 'is the "
            "Kano ILS working today', 'can I get a clearance to land at Port "
            "Harcourt'.\n"
            "Decide on what the answer is, not on a word in the question."
        ),
    )

    out_of_scope_reason: Optional[Literal[
        "transient_status",
        "current_weather",
        "atc_clearance",
        "planning_or_flow",
        "charges",
        "outside_nigeria",
        "other",
    ]] = Field(
        None,
        description=(
            "Fill this when intent is 'out_of_scope' OR needs_live_source is "
            "true. Null otherwise.\n"
            "The test is PERMANENCE. The AIP publishes information of lasting "
            "character, true until the next amendment; only a state that "
            "changes from day to day belongs to someone else. Words like now, "
            "current or today do not decide it.\n"
            "transient_status — whether a runway, taxiway, apron, aerodrome, "
            "navaid or facility is open, closed, serviceable, usable or "
            "restricted at the moment; works in progress; active NOTAMs. "
            "NOTAM territory. NOT the runway in use, which AD 2.22.2 publishes.\n"
            "current_weather — an observed or forecast weather VALUE: a METAR, "
            "SPECI or TAF report, the wind, visibility, ceiling or QNH right "
            "now. NOTE the AIP's MET POLICY in AD 2.11 (TAF validity, trend "
            "type, briefing hours) and published values such as the aerodrome "
            "reference temperature are NOT this and are IN scope.\n"
            "atc_clearance — a clearance or instruction for a particular "
            "flight, which only a controller can give. Published clearance "
            "rules and procedures are IN scope.\n"
            "planning_or_flow — a slot, delay, flight status or schedule for a "
            "particular flight. Published flight-plan filing procedures are "
            "IN scope.\n"
            "charges — a price the AIP does NOT publish: fuel price, handling "
            "quotes, ticket prices. NOTE aerodrome and air navigation charges "
            "ARE published in GEN 4 (landing, parking, passenger service, "
            "en-route, terminal navigation) and are IN scope — classify those "
            "as national_lookup and set gen4_charge instead.\n"
            "outside_nigeria — an aerodrome or airspace outside Nigeria.\n"
            "other — out of scope for none of the above reasons.\n"
            "Choose the reason the question actually turns on, not a word that "
            "happens to appear in it."
        ),
    )

    # Which GEN 4 subsection a charges question belongs to. Read by main.py to
    # fetch that subsection BY NAME and show it verbatim, rather than leaving a
    # fee schedule to vector search and synthesis, which could swap the
    # international and domestic rates or the day and night rates — the same
    # juxtaposition hazard AD 2.12 and AD 2.17 have.
    gen4_charge: Optional[Literal["aerodrome", "air_navigation"]] = Field(
        None,
        description=(
            "Set ONLY when the question asks about a charge or fee the Nigerian "
            "AIP publishes in GEN 4. Null otherwise.\n"
            "aerodrome — GEN 4.1 aerodrome charges: landing fees, parking, "
            "avio bridge, passenger service charge, cargo/port charges, the "
            "fuel throughput charge levied on oil marketing companies, CUTE "
            "and check-in counter charges.\n"
            "air_navigation — GEN 4.2 air navigation services charges: "
            "en-route (route) charges, overflight charges, terminal navigation "
            "charge (TNC), charges for ATS outside normal hours.\n"
            "NOT a GEN 4 charge, so null: the price of fuel itself, handling "
            "agent quotes, any price at an airport outside Nigeria. GEN 4 "
            "publishes one national schedule, so a charges question naming a "
            "Nigerian aerodrome still gets gen4_charge set."
        ),
    )

    icao_code: Optional[str] = Field(
        None,
        description=(
            "ONLY fill this if the user literally typed a 4-letter code starting "
            "with 'DN' (e.g. 'DNAA'). NEVER infer a code from a city or airport "
            "name — leave it null and put the name in aerodrome_name instead."
        ),
    )

    aerodrome_name: Optional[str] = Field(
        None,
        description="The city or airport name the user mentioned, copied verbatim. Do not convert it to a code.",
    )

    procedure_type: Optional[str] = Field(
        None,
        description="e.g. 'ILS', 'RNAV', 'SID', 'STAR', 'Tower', 'ATIS'. Leave null if none.",
    )

    runway: Optional[str] = Field(
        None,
        description="Runway designator if mentioned, e.g. '18L', '22'. Leave null if none.",
    )

    filter_part: Literal["GEN", "ENR", "AD"] = Field(
        description="Best guess at the AIP structural area. Treated as a hint only, not a hard constraint.",
    )

    ad2_subsection: Optional[str] = Field(
        None,
        description=(
            "If the question is about a specific aerodrome, which AD 2.x subsection "
            "holds the answer? Reply with the number only (e.g. '2.12'), or null if "
            "it isn't aerodrome-specific or you are unsure. Do NOT guess — null is "
            "always better than a wrong section. A vague query with no "
            "distinctive term ('apron info', 'notes at X', 'details for X') has "
            "NO correct subsection: return null and let retrieval decide. "
            "Measured: guessing on these sent 10 queries across five different "
            "subsections to 2.5, because 'facilities' is semantically close to "
            "everything.\n"
            "2.1 location indicator/name | 2.2 ARP coordinates, elevation, magnetic "
            "variation, reference temperature, operator, AFTN | 2.3 operational hours, "
            "customs, immigration, health, AIS, ARO, MET briefing, fuelling hours | "
            "2.4 handling services, fuel/oil types, hangars, repairs, cargo, de-icing | "
            "2.5 passenger facilities FOR TRAVELLERS ONLY: hotels, restaurants, "
            "tourist office, banks, post office, ground transport into town, "
            "medical facilities for passengers. This section is NOT a general "
            "'facilities' bucket — do NOT choose it for aircraft or airfield "
            "infrastructure. Apron, taxiway and stand information is 2.8; "
            "fuelling, hangars, cargo and repairs are 2.4; fire and rescue is "
            "2.6; lighting is 2.14/2.15. If a query is vague ('facilities', "
            "'info', 'notes', 'details') return null rather than 2.5 | "
            "2.6 rescue and fire fighting, RFF category, removal of disabled aircraft | "
            "2.7 seasonal availability, clearing | 2.8 aprons, taxiways, apron/taxiway "
            "surface and strength, altimeter/VOR/INS check locations | 2.9 surface "
            "movement guidance, markings, stop bars, stand ID signs | 2.10 aerodrome "
            "obstacles | 2.11 meteorological information, MET office, TAF, trend "
            "forecast | 2.12 runway physical characteristics: designation, true "
            "bearing, dimensions, length, width, surface, strength/PCN, threshold "
            "coordinates and elevation, slope, stopway, clearway, strip | 2.13 declared "
            "distances TORA/TODA/ASDA/LDA | 2.14 approach and runway lighting, PAPI, "
            "threshold/centreline/edge/end lights | 2.15 other lighting, ABN/IBN "
            "beacon, wind direction indicator, secondary power supply | 2.16 helicopter "
            "landing area, TLOF, FATO | 2.17 ATS airspace: CTR/TMA lateral and vertical "
            "limits, airspace classification, ATS unit call sign, transition altitude | "
            "2.18 ATS communication facilities, tower/ground/approach/ATIS frequencies "
            "and call signs | 2.19 radio navigation and landing aids: VOR, DME, ILS, "
            "NDB, localizer, glide path — frequencies, idents, positions | 2.20 local "
            "aerodrome regulations, taxiing limitations, parking, training flights | "
            "2.21 noise abatement | 2.22 flight procedures: runway in use, holding, "
            "letdown, missed approach, approach and take-off minima, OCA/H, circling, "
            "radar procedures, VFR procedures, PBN | 2.23 additional information, bird and wildlife "
            "hazards | 2.24 charts"
        ),
    )

    @field_validator("icao_code")
    @classmethod
    def _norm_icao(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = v.strip().upper()
        return v or None

    @field_validator("runway", "aerodrome_name", "procedure_type")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None
