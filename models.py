"""
models.py — internal data structures passed between the layers.
(Pydantic schema for the LLM lives separately in schemas.py.)
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Resolution:
    """Outcome of deterministic ICAO/part resolution."""
    icao: Optional[str] = None          # canonical ICAO, or None for national/en-route
    label: str = ""                     # human label for citations
    part: str = "GEN"                   # primary AIP part to search
    reference: Optional[str] = None     # reference_tag to search; None => national tags
    is_national: bool = False
    aerodrome_hint: Optional[str] = None  # canonical name for embedding, even when not pinning to AD
    ambiguous: List[str] = field(default_factory=list)  # >1 candidate ICAO
    unresolved: bool = False            # named an aerodrome we don't have
    reason: str = ""
    # SCOPE. Defaults keep every existing caller working: a Resolution that
    # names an aerodrome is scope_kind "AD" with scope_id = icao, exactly as
    # before.
    #
    # These exist because `icao` cannot name a waypoint, an airway or a danger
    # area. A pilot asked "Where is TEMSA?" and was told it is not in the
    # Nigerian AIP; it is, on seven pages, but resolve() had nowhere to put a
    # non-aerodrome identity and so returned unresolved.
    scope_kind: str = ""                # AD | ENR_AREA | ENR_POINT | ...
    scope_id: str = ""                  # DNMM | DND45 | TEMSA | UT467

    def __post_init__(self):
        if not self.scope_kind and self.icao:
            self.scope_kind, self.scope_id = "AD", self.icao


@dataclass
class AIPResult:
    content: str
    similarity: float
    chart_url: Optional[str] = None
    aip_section: Optional[str] = None     # e.g. "AD 2", "ENR 1.1"
    reference_tag: Optional[str] = None   # ICAO code, or "AIRSPACE" / "NATIONAL"


@dataclass
class SearchOutcome:
    results: List[AIPResult] = field(default_factory=list)
    max_similarity: float = 0.0
    used_part: Optional[str] = None
    used_reference: Optional[str] = None
    abstained: bool = True
    reason: str = ""


@dataclass
class ChartRef:
    url: str
    procedure_type: Optional[str] = None
    runway: Optional[str] = None
    icao_code: Optional[str] = None
    is_pdf: bool = False
