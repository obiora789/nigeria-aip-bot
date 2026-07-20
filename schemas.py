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
            "always better than a wrong section.\n"
            "2.1 location indicator/name | 2.2 ARP coordinates, elevation, magnetic "
            "variation, reference temperature, operator, AFTN | 2.3 operational hours, "
            "customs, immigration, health, AIS, ARO, MET briefing, fuelling hours | "
            "2.4 handling services, fuel/oil types, hangars, repairs, cargo, de-icing | "
            "2.5 passenger facilities, hotels, restaurants, transport, medical, bank | "
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
            "2.21 noise abatement | 2.22 flight procedures: holding, letdown, missed "
            "approach, approach and take-off minima, OCA/H, circling, radar procedures, "
            "VFR procedures, PBN | 2.23 additional information, bird and wildlife "
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
