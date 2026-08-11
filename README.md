# Vannie — Nigeria AIP Reference Assistant

A Telegram bot that answers questions about the **Nigerian AIP** (currently
`AIRAC AMDT 03/2026`) — frequencies, runway data, declared distances, navaids,
charts, ICAO mapping, **and now ENR**: danger/restricted/prohibited areas,
en-route waypoints, ATS routes, en-route navaids and controlled airspace — for
pilots and dispatchers.

**Vannie is a reference aid, NOT an operational source.** Every answer carries a
disclaimer to verify against the official AIP and current NOTAMs before flight.
The whole system is built around one rule: **never state a wrong operational
value.** Where it can't be confident, it abstains, shows the verbatim source, or
defers to the chart — it does not guess.

---

## How an answer is produced

```
Telegram → webhook (secret check → dedup → throttle → fast 200 ack)
        → extract params (LLM, structured) + deterministic backstops
        → deterministic ICAO/section/SCOPE resolution (from our own data)
        → conversation context (slot-fill / follow-up, surfaced not silent)
        → route:
            greeting / help            → static copy
            structure question         → deterministic ToC map (toc.py)
            cross-aerodrome enumeration → structured-facts boundary (facts.py)
            ICAO ↔ city mapping         → deterministic table
            aerodrome-or-navaid ident   → asked with buttons (VOR idents that
                                          are ALSO published ENR navaids, e.g.
                                          "ABC" = Abuja AND the ABC VOR/DME)
            chart request              → plate(s) [+ scoped procedures text]
            structured lookup          → aip_structured exact key lookup
            never-synthesize section   → aip_facts field-level lookup, else
                                          verbatim (AD 2.22, AD 2.17, all ENR)
            everything else            → vector search (max-sim gated)
                                          → grounded synthesis + per-excerpt verifier
                                          → verbatim fallback / faithful abstain
        → log the query + outcome (observability) → 👍/👎 feedback buttons
```

A **scope** is not always an aerodrome. `resolver.py` resolves a query to
`(scope_kind, scope_id)` — `("AD", "DNMM")` for Lagos, but also
`("ENR_AREA", "DND45")` for a danger area, `("ENR_POINT", "TEMSA")` for a
waypoint, `("ENR_NAVAID", "ABC")` for an en-route navaid. `aip_facts` is keyed
the same way, so retrieval is confined to one scope regardless of what kind of
thing it is — the guarantee that a fact can never be returned for a different
entity holds for a waypoint exactly as it does for a runway.

## Module map

### Bot runtime

| File | Role |
|---|---|
| `config.py` | Settings, env vars, models, thresholds, AIRAC cycle, safety copy, feature toggles |
| `models.py` | Internal dataclasses (Resolution, AIPResult, SearchOutcome, ChartRef) |
| `schemas.py` | Strict LLM extraction schema (**no ICAO guessing**) + `GroundedAnswer`/`GroundedFact` (every fact declares its own `source_excerpt`) |
| `agent.py` | The LLM boundary: structured extraction + embeddings, plus deterministic backstops (fabricated-ICAO drop, chart-intent force, MET/comms reroute) |
| `resolver.py` | **Deterministic ICAO/section/scope resolution** from our own data; aliases, verified VOR idents, `published_entities()` (membership-based recognition for ENR idents/waypoints/areas — no shape regex, no stop-list), per-field query enrichment, `build_search_text` |
| `entity_scope.py` | **Never-synthesize sections**, keyed on section identity alone (no content vocabulary). AD 2.22 and AD 2.17 and every ENR section: any free-form synthesis over these could splice one entity's value onto another's citation. `blocking_section()` is the single chokepoint every routing layer checks. |
| `database.py` | Vector search with **max-similarity gating**; `get_charts_smart` (direct table, synonym + side-aware runway match); `get_section_text`; `get_declared_distances`; `search_facts` / `search_facts_scoped` (field-level `aip_facts` lookup, aerodrome or ENR scope); `find_aip_scope` / `list_scope_ids` (exact name→scope lookup and bulk scope-id listing — the authoritative sets `resolver.published_entities()` is built from) |
| `synthesize.py` | **Grounded synthesis + per-excerpt deterministic verifier** — every fact is checked against the ONE excerpt it declares (`source_excerpt`), not a flattened blob of everything retrieved; prose facts with no numbers require a verbatim match too; **restriction/authorisation guard** (`_RESTRICTION_RE`) refuses synthesis entirely for night-flying/curfew-type rules; **minima carve-out** (never synthesizes a decision height) |
| `responder.py` | Extractive replies with citation + AIRAC + disclaimer; cites deterministically from each fact's `source_excerpt` (never a post-hoc word-overlap re-guess); focused single-chunk source blocks; **side-aware runway matching** (18L ≠ 18R) |
| `procedures.py` | **Verbatim approach-procedure sectioniser** — scopes to the exact approach, splits Holding/Letdown/Missed, all-three-or-plate fallback (toggle-gated) |
| `ad222_respond.py` | Approach-procedure + chart join: parses AD 2.24's own chart index by reference number, pairs the matching plate with the scoped AD 2.22 procedure text |
| `toc.py` | Deterministic AIP table-of-contents for "which part covers X" |
| `facts.py` | Cross-aerodrome enumeration handled as a documented boundary |
| `telegram.py` | Delivery, photo-vs-document charts, 👍/👎 keyboard, callback ack, webhook secret |
| `memory.py` | Short-term conversation context (slot-fill + follow-up carry), TTL-bounded. `save_scope_pending` / `save_chart_pending` write dedicated shapes per clarification kind so a button tap's callback guard (`kind` + `qid`) can always match. `clear()` — the `/reset` command — **deletes** the row rather than nulling it, since a row of nulls still satisfies "context exists". |
| `cache.py` | Redis-backed dedup/throttle with in-memory fallback (optional) |
| `observability.py` | Query log, startup/deep healthcheck, `/dashboard` renderer, triage helpers |
| `alerting.py` | Throttled operator alerts on health degrade + recovery notices |
| `main.py` | Webhook + pipeline + `/health`, `/health/deep`, `/dashboard`; background health monitor |
| `triage.py` | CLI weekly-review tool for the query log (`--open`, `--mark`, `--icao`) |
| `eval_set.py` / `e2e.py` / `harness.py` / `test_offline.py` | Evaluation + offline tests |

### Layer 1 — extraction foundation (single source of truth)

| File | Role |
|---|---|
| `aip_structure.py` | Page-boundary table for all sections (36 aerodromes, 4 heliports, GEN, special chart sections). `classify_page.py` and `extract_charts.py` import from this single object, so the two pipelines can never disagree about a boundary. |
| `classify_page.py` | Classifies every page: `AD_CONTENT / CHART_PLATE / CHART_INDEX / BLANK / AD_SPECIMEN / AD3_HELIPORT_PROC / ENR_CONTENT / GEN_CONTENT / ...` |
| `segment_page.py` | Splits each aerodrome's page range into per-subsection segments, handling subsections that span multiple physical pages |
| `extractor_base.py` | Shared `SubsectionExtractor` contract + helpers: gutter-based label:value splitting, whole-row text capture, cross-page word ordering, PUA-glyph cleanup |

### Layer 2 — one extractor per AD 2.x subsection

| File | Subsection |
|---|---|
| `ad21_extractor.py` | AD 2.1 — Aerodrome location indicator and name |
| `ad22_extractor.py` | AD 2.2 — Geographical and administrative data |
| `ad23_extractor.py` | AD 2.3 — Operational hours |
| `ad24_extractor.py` | AD 2.4 — Handling services and facilities |
| `ad25_extractor.py` | AD 2.5 — Passenger facilities |
| `ad26_extractor.py` | AD 2.6 — Rescue and fire fighting |
| `ad27_extractor.py` | AD 2.7 — Seasonal availability, clearing |
| `ad28_extractor.py` | AD 2.8 — Aprons, taxiways and check locations |
| `ad29_extractor.py` | AD 2.9 — Surface movement guidance and markings |
| `ad210_extractor.py` | AD 2.10 — Aerodrome obstacles (column-aware text) |
| `ad211_extractor.py` | AD 2.11 — Meteorological information provided |
| `ad212_extractor.py` | AD 2.12 — Runway physical characteristics (per-runway-end) |
| `ad213_extractor.py` | AD 2.13 — Declared distances |
| `ad214_extractor.py` | AD 2.14 — Approach and runway lighting (per-runway-end) |
| `ad215_extractor.py` | AD 2.15 — Other lighting, secondary power supply |
| `ad216_extractor.py` | AD 2.16 — Helicopter landing area |
| `ad217_extractor.py` | AD 2.17 — ATS airspace |
| `ad218_extractor.py` | AD 2.18 — ATS communication facilities (per-service) |
| `ad219_extractor.py` | AD 2.19 — Radio navigation and landing aids (per-navaid) |
| `ad220_extractor.py` | AD 2.20 — Local aerodrome regulations (column-aware) |
| `ad221_extractor.py` | AD 2.21 — Noise abatement procedures |
| `ad222_extractor.py` | AD 2.22 — Flight procedures (page-driven; needs `_span22.json`) |
| `ad223_extractor.py` | AD 2.23 — Additional information (column-aware text) |

Every extractor has a matching `validate_adXX.py` proof script that runs it
against all 36 standard aerodromes. Full design rationale for each subsection,
and the recurring bug patterns found while building this layer, are documented
in `PROJECT_SUMMARY.md`.

### Layer 2b — ENR (en-route) extractors

AD 2.x is per-aerodrome. ENR is per-*entity* — a danger area, a waypoint, an
airway, a navaid — and none of those has an ICAO code, which is why they were
unreachable ("Where is TEMSA?" → "not in the Nigerian AIP") until `aip_facts`
was generalised from `icao_code` to `(scope_kind, scope_id)` (see
`sql/12_scope_generalisation.sql`).

| File | Subsection | Entity | Extraction approach |
|---|---|---|---|
| `enr51_extractor.py` | ENR 5.1 | Prohibited/restricted/danger areas | Row-delimited by the AIP's own `DNP`/`DNR`/`DND` id scheme; family comes from the **id letter**, never the nearest heading (a heading can trail into the wrong block) |
| `enr44_extractor.py` | ENR 4.4 | Significant points (waypoints) | Line-parsed; the id is the row delimiter and is unique — zero collisions with ICAO codes, city aliases or VOR idents, checked directly |
| `enr21_extractor.py` | ENR 2.1 | FIR / UIR / sector / TMA / CTA | Entity set **discovered from the AIP's own "X consists of A and B" declarations**, not pattern-matched — a name-shape heuristic once let "Kaduna TMA" (a phrase inside Kano TMA's own lateral-limits text) become a fake entity that stole Kano's class and vertical limits |
| `enr41_extractor.py` | ENR 4.1 | En-route navaids | Keyed on **ident**, not station — one station block can publish several navaids (Benin: VOR/DME "BEN" + locator "BE"), and keying on the station name would let a query for one's frequency return the other's |
| `enr3x_extractor.py` (`ENR3XExtractor`) | ENR 3.1 / 3.2 | ATS routes | **Band-sliced by x-position** (via `pdfplumber`, not `pypdfium2`) — the seven-column table interleaves when flattened to plain text, and three successive line-based heuristics each produced a point at another point's coordinates before the column slice made that structurally impossible. A route record is an **ordered list of ENR 4.4 point names**; coordinates are captured for cross-checking only, never as the authoritative position. |
| `enr3x_extractor.py` (`ENR33Extractor`) | ENR 3.3 | Free Route Airspace DCT combinations | Also band-sliced. Not a route table — no route designator exists, so `scope_id` is **derived** from the endpoints (`"KELAK-POLTO"`) and stated as such. Each combination is checked for **self-agreement**: the AIP states the routing twice (header string + point rows), and a mismatch means a point row was misattributed. |

Each has a matching `validate_enrXX.py`. Two things these validators check that
the AD ones don't need to, because ENR's entities interleave across sections in
a way aerodromes don't:

- **Cross-section agreement** — `validate_enr3x.py` and `validate_enr33.py`
  compare every route point's coordinates against **ENR 4.4's own extraction**
  of the same point. This is what caught two confirmed AIP typos (a 30
  arc-second discrepancy at TEGDA, a digit missing from GANDA's longitude) —
  neither is catchable by any single-section check, because both halves of the
  comparison come from different sections.
- **`--spot-all` / `--spot <name>`** — every ENR validator can print full
  records beside their source column for a human read. This is not optional
  ceremony: every automated PASS in this layer has, at some point, been true
  while a real defect was present (a column header stored as a value, an area
  filed under the wrong family, a navaid published at another navaid's
  position). Budget ~10 min per section before trusting a cycle's ENR data.

### Ingestion

| File | Role |
|---|---|
| `build_span22.py` | Generates `_span22.json` — the AD 2.22 page-span map (start/end page per aerodrome). Required before AD 2.22 can be ingested at all; without it, approach procedures are silently skipped. |
| `vectorise_aip_v3.py` | **Ingestion (AD 2, all 36 aerodromes)** — one pass per aerodrome: load pages once, segment once, fan out to every AD 2.x extractor. Writes one `aip_knowledge_base` chunk per `(aerodrome, subsection)` and one typed record per entity into `aip_structured`. AD 2.22 is handled separately within the same run (page-driven, via `_span22.json`, since its content pages classify as `CHART_PLATE` and are invisible to normal segmentation). |
| `vectorise_aip_v2.py` | Ingestion for **GEN / ENR / AD 1.x / AD 3** — its AD 2 branch is superseded by `vectorise_aip_v3.py`; retained only for the non-AD-2 page-walk until that's harvested into its own script. |
| `extract_charts.py` | Populates the `aip_charts` catalogue (chart retrieval + plate-page exclusion). **Must run before either vectoriser.** |
| `extract_declared_distances.py` | Superseded by `ad213_extractor.py` for new ingests, but `database.py` still reads its `aip_declared_distances` table directly — keep until that call site is migrated to `aip_structured`. |

---

## Safety design (the guardrails)

- **No wrong-airport hallucination.** ICAO is resolved deterministically from our
  own data + a verified alias list, never guessed by the LLM. Ambiguous name →
  ask; unknown name → refuse.
- **Grounded synthesis with a per-excerpt verifier.** Every `GroundedFact`
  declares the exact excerpt it was copied from (`source_excerpt`); the verifier
  checks each fact's value against THAT excerpt specifically — number membership
  AND a verbatim substring match — never against a flattened blob of everything
  retrieved. This closes a real, confirmed misattribution: a genuinely correct
  number from one excerpt used to verify successfully even when the reply's
  citation pointed at a different, wrong section. Prose facts with no numbers
  (a stated rule or restriction) now require verbatim verification too — previously
  they skipped the check entirely, since there were no digits to test. Any
  violation → reject → fall back to verbatim. Fails safe.
- **Citations are deterministic, not re-guessed.** `responder._source_block` cites
  from the fact's own `source_excerpt`, not a post-hoc word/number-overlap
  re-ranking of all retrieved chunks — the mechanism that once produced a wrong
  citation (a real answer attributed to AD 2.20 when its governing text was
  actually AD 2.22.5.1).
- **No synthesis for restriction/authorisation rules.** Night-flying bans,
  curfews, and similar rules are never LLM-paraphrased (`_RESTRICTION_RE`) — a
  bare numbered/lettered list item quoted without its governing clause ("unless
  authorised by...") can reverse the rule's meaning. Shown verbatim instead.
- **Verbatim for the highest-stakes values.** Approach minima / decision heights
  are never synthesized — the table is shown as-is (`synthesize._MINIMA_RE`).
- **Approach procedures are scoped, not spliced.** `procedures.py` only shows
  Holding/Letdown/Missed when it can pin them to one approach and all three parse
  cleanly; otherwise it defers to the plate. The plate always follows anyway.
- **Multi-entity tables never cross-attribute.** The Layer 2 extractors for
  dense tables (runways, navaids, comms, lighting) track a "currently active
  entity" while walking the source text, so a value can only ever attach to the
  entity whose own header line most recently introduced it — making
  cross-entity misattribution structurally impossible at the extraction layer,
  ahead of anything synthesis or retrieval could do about it.
- **Faithful abstention.** "Not in the AIP" means not found after fallback, and
  is shown as an honest refusal — not a bad guess.
- **Never-synthesize sections, keyed on identity not vocabulary.**
  `entity_scope.blocking_section()` is a single chokepoint every routing layer
  checks before any free-form synthesis: AD 2.22, AD 2.17, and every ENR
  section. Two earlier attempts tried to detect *which content* was unsafe
  (an approach-header regex, then a procedure-label vocabulary) and each
  missed real cases — measured at 30% and then still blind to five whole
  categories of AD 2.22 content (PBN, departure, emergency, radar, VFR
  procedures). Keying on the section's *identity* instead needs no vocabulary
  and has no coverage gap by construction.
- **A scope ambiguity is asked, with buttons, not assumed.** A VOR ident can
  name both an aerodrome and a separately published ENR navaid — "ABC" is
  Abuja's ident *and* the ABC VOR/DME (116.3 MHz) in ENR 4.1. Both readings are
  correct, so `resolver.resolve()` returns `ambiguous_kind="scope"` and
  `telegram.clarify_scope_kb()` asks; the button carries the chosen scope in
  its callback data, so nothing is re-parsed. Raising the question **never**
  pins a default answer as conversation context — an earlier version wrote
  `last_icao` when asking, which silently answered the *next* identical
  question as the aerodrome.
- **Currency + citation + disclaimer on every reply.**

---

## Data layer

Three Supabase tables back the bot:

- `aip_knowledge_base` — embedded AIP text chunks (`text-embedding-3-small`,
  1536-dim). For AD 2 (all 36 aerodromes), built by `vectorise_aip_v3.py`: one
  chunk per `(aerodrome, subsection)`, so a chunk can never straddle a
  header/value boundary or mix two subsections' values. GEN/ENR/AD 1.x/AD 3
  still come from `vectorise_aip_v2.py`'s column-aware page-walk.
- `aip_structured` — typed per-entity records (jsonb), keyed on
  `(icao_code, subsection, record_index)`. The exact-lookup counterpart to the
  vector store: a runway's TORA/TODA/ASDA/LDA or a navaid's frequency is a key
  lookup here, not a similarity search over prose.
- `aip_facts` — **field-level** facts (one row per atomic value, e.g. one
  runway's TORA, one navaid's frequency), embedded for semantic retrieval.
  Keyed on `(scope_kind, scope_id, subsection, entity, label)` — see
  `sql/12_scope_generalisation.sql`, `13_scope_cutover.sql`, and
  `14_add_navaid_scope.sql` for how this was generalised from an
  aerodrome-only `icao_code` key. `scope_kind` is one of `AD`, `ENR_AREA`,
  `ENR_POINT`, `ENR_ROUTE`, `ENR_AIRSPACE`, `ENR_NAVAID`, `ENR_FRA_DCT`, `GEN`
  (enumerated by a check constraint — an unrecognised scope must fail loudly,
  not silently create facts nothing can retrieve). Built by
  `build_fact_index.py`, which reads `aip_structured` for AD and each
  `enrXX_extractor.py` directly (via `--enr <subsection>`) for ENR.
- `aip_charts` — the chart/plate catalogue (built by `extract_charts.py`).

**Re-ingesting AD 2** (after any extractor change, or a new AIRAC cycle):

Prerequisites, one-time or after any extractor change:
1. `aip_structured` exists with `unique (icao_code, subsection, record_index)`
2. `aip_knowledge_base`'s unique constraint includes `aip_section` — required
   because multiple AD 2.x subsections can legitimately start on the same
   physical page on short-document aerodromes
3. `_span22.json` exists (`python build_span22.py Complete_AIP2026.pdf` if not)

```sql
DELETE FROM aip_knowledge_base WHERE aip_part = 'AD' AND aip_section LIKE 'AD 2%';
DELETE FROM aip_structured;
```
```bash
python extract_charts.py                     # must run first
python validate_ad21.py Complete_AIP2026.pdf  # ...through validate_ad223.py — all must pass
python vectorise_aip_v3.py
```

**Re-ingesting GEN/ENR/AD 1.x/AD 3** (unchanged):

```sql
DELETE FROM aip_knowledge_base WHERE aip_part != 'AD' OR aip_section NOT LIKE 'AD 2%';
```
```bash
rm vectorise_v2_progress.json
python vectorise_aip_v2.py
```

**Building/rebuilding `aip_facts`:**

```bash
python build_fact_index.py --force              # AD, all 36 aerodromes
python build_fact_index.py --enr 5.1 --pdf Complete_AIP2026.pdf
python build_fact_index.py --enr 4.4 --pdf Complete_AIP2026.pdf
python build_fact_index.py --enr 2.1 --pdf Complete_AIP2026.pdf
python build_fact_index.py --enr 4.1 --pdf Complete_AIP2026.pdf
python build_fact_index.py --enr 3.1 --pdf Complete_AIP2026.pdf
python build_fact_index.py --enr 3.2 --pdf Complete_AIP2026.pdf
python build_fact_index.py --enr 3.3 --pdf Complete_AIP2026.pdf
```

`--dry-run --limit N` previews facts without writing. Every run reports rows
**actually written**, never rows attempted — an earlier version reported
"indexed 125 facts" after every upsert had failed, so a completely broken run
looked successful. `NOT WRITTEN` lines and upsert errors are always shown, and
a run is idempotent: re-running the same command only adds what's missing.

---

## Observability & feedback

- Every query writes a row to `vannie_query_log` (query, intent, ICAO, path,
  similarity, charts). Non-confident outcomes auto-flag `needs_review`.
- **Feedback:** 👍/👎 buttons under substantive answers; a 👎 flags that query for
  review — real wrong answers surface themselves.
- **Triage:** `python triage.py` (`--days`, `--icao`, `--open`, `--all`,
  `--mark <id> --note "…"`). Mark items handled so the queue converges.
- **`/reset` (also `/clear`, `/forget`):** a Telegram command, deliberately
  **not** operator-gated — a pilot who has hit stale conversation context (a
  wrong aerodrome carried into an unrelated question) needs a way out that
  doesn't depend on knowing why. Calls `memory.clear()`, which **deletes** the
  context row rather than writing nulls into it.
- **Stress testing:** `build_stress_set.py` generates AIP-grounded test cases
  from `aip_facts` (not from observed bot behaviour — a set built from what the
  bot currently does can encode a bug as a passing test). `fix_stress_set.py`
  repairs known generator defects (ambiguous questions relabelled
  `expected_behaviour=clarify`, extractor-internal labels dropped, section
  format normalised). `run_stress_set.py` drives the **real** `main.process()`
  with Telegram sends captured in memory — not a second copy of the decision
  flow, since an earlier mirror (`e2e.run_pipeline`) silently fell behind and
  handled 2 of 11 possible statuses. `measure_intent.py` isolates the
  extraction/classification layer alone (`--focus` re-tests only prior
  failures, far cheaper than a full re-run) for tuning `agent._SYSTEM` without
  the cost of a full pipeline run.
- **Dashboard:** `GET /dashboard?token=DASHBOARD_TOKEN` — live, read-only, no
  third-party data egress. `GET /health/deep?token=…` runs the credential check.
- **Alerting:** the background monitor re-checks OpenAI + Supabase on an interval
  and pings `ADMIN_CHAT_ID` on degrade/recovery. Boot logs a clear PASS/FAIL line.

---

## Environment variables

**Required (won't boot without them):**
`OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY` (use the **service_role** key),
`TELEGRAM_BOT_TOKEN`.

**Set for this deploy:**
`TELEGRAM_WEBHOOK_SECRET` (webhook auth), `DASHBOARD_TOKEN` (enables `/dashboard`
+ `/health/deep`), `ADMIN_CHAT_ID` (your Telegram id, for alerts), `PDF_PATH`
(source PDF path — read directly by both vectorisers and `build_span22.py`).

**Optional:**
`AIRAC_CYCLE` (**change every cycle**), `REDIS_URL` (durable/shared dedup+throttle),
`CONTEXT_TTL_MIN` (10), `DEEP_CHECK_INTERVAL_SEC` (600), `ALERT_MIN_INTERVAL` (900),
and feature toggles (default on unless noted): `SYNTHESIS_ENABLED`,
`QUERY_LOG_ENABLED`, `CONTEXT_ENABLED`, `ALERT_ENABLED`, and
`PROCEDURES_TEXT_ENABLED` (**off by default** — see below).

Tuning knobs (safe defaults): `MATCH_LIMIT`, `SIMILARITY_THRESHOLD`, `MAX_CHARTS`,
`SYNTHESIS_CONTEXT_CHUNKS`, `PER_CHAT_COOLDOWN_SECONDS`, `DEDUP_TTL_SEC`,
`EMBEDDING_MODEL`, `EXTRACTION_MODEL`, `SYNTHESIS_MODEL`.

---

## Database migrations

Run in Supabase in order (all idempotent):

```
sql/match_aip_text_advanced.sql   -- vector search RPC
sql/charts_airac.sql              -- charts + AIRAC
sql/05_query_log.sql              -- observability log
sql/06_query_log_reviewed.sql     -- triage convergence (reviewed flag)
sql/07_conversation_context.sql   -- short-term memory
sql/08_query_feedback.sql         -- 👍/👎 (qid + feedback)
sql/10_aip_declared_distances.sql -- legacy structured declared distances (still read by database.py)
```

Plus the two `aip_structured`/`aip_knowledge_base` constraint statements under
**Data layer** above, for the Layer 2 rebuild.

---

## Deploy

CI/CD is wired: push to `main` → GitHub Actions runs compile/import/offline tests
→ on green, triggers the Render deploy hook. **Turn Render auto-deploy OFF** so the
gate is the only path to production.

One-time setup:
- GitHub repo secrets: `RENDER_DEPLOY_HOOK`; and for the scheduled eval,
  `OPENAI_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`.
- Render: set the env vars above; Render auto-deploy → **No**.
- Register the webhook with the secret:

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://YOUR_HOST/webhook" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

Local run:

```bash
pip install -r requirements.txt
cp .env.example .env   # fill it in
uvicorn main:app --host 0.0.0.0 --port 8000
```

`requirements.txt` includes `pypdfium2` (fast full-document page scanning),
`pdfplumber` (word-**position** extraction — required for the ENR 3.x band
slice, which reads column x-positions directly), and `PyMuPDF` (imported as
`fitz`; used by all 23 `validate_ad2*.py` scripts). `cryptography` is pinned
to a range with published binary wheels (`>=42.0,<44.0`) — left unpinned, pip
can resolve a version whose source build needs a newer Cargo/Rust toolchain
than a given machine has, which is a local build failure rather than anything
wrong with the package itself.

---

## AIRAC currency

The corpus is a single AIRAC cycle. AIP data changes on a 28-day cycle, and a
reference tool serving stale data is worse than none.

**There is currently no automated expiry check.** `AIRAC_CYCLE` is a config
string; nothing compares it to today's date, checks it against an expiry date,
or warns as one approaches. A pilot has no signal that a reply's data may have
been superseded. This is the highest-priority item not yet built — see
`AIRAC_UPGRADE_FRAMEWORK.md` for the planned `cycles/<id>/manifest.json`
(carries `effective`/`expires` dates) and the expiry-banner design.

Each cycle, once the mechanism above exists to gate on:

1. `python extract_charts.py`
2. `python build_span22.py Complete_AIP2026.pdf` (AD 2.22 spans can shift page numbers)
3. Run all `validate_adXX.py` scripts — every one must pass before re-ingesting
4. `python vectorise_aip_v3.py` (AD 2), then `python vectorise_aip_v2.py` (GEN/ENR/AD 1.x/AD 3)
5. Run all six `validate_enrXX.py` scripts against the new PDF
6. Rebuild `aip_facts`: `python build_fact_index.py --force`, then
   `--enr <subsection>` for each of `5.1 4.4 2.1 4.1 3.1 3.2 3.3`
7. **Validate a sample against the new AIP** — spot-check the highest-stakes
   subsections (AD 2.12, 2.13, 2.18, 2.19) across a few aerodromes, **and**
   `--spot-all` a sample of each ENR section. Every ENR validator has, at some
   point, reported PASS while a real defect was present — this step is not
   optional.
8. Update `AIRAC_CYCLE` (and, once built, the manifest's `effective`/`expires`)

This validation + currency pipeline is human-owned and is the most important
ongoing safety work. Full framework — structural diffing before extraction,
expected-count regression checks, cross-section comparison, the five-gate
release process — is in `AIRAC_UPGRADE_FRAMEWORK.md`.

---

## Approach-procedure text (validate before enabling)

`PROCEDURES_TEXT_ENABLED` is **off by default**. When off, approach requests get
the plate (with a pointer). When on, Vannie also shows the verbatim
Holding/Letdown/Missed-Approach text, scoped to the requested approach, joined
to its chart via `ad222_respond.py`'s reference-number match against AD 2.24's
own index.

Before enabling it, validate on the **real re-ingested data** across a sample of
aerodromes (large, small, multi-approach): confirm the right approach is scoped
**and** that each section's text matches the plate/PDF with no missing clause. The
built-in guardrails catch structural failures (wrong/ambiguous approach, missing
section → plate) but cannot catch a section that parses yet drops a clause — that's
what the human check is for. Only then set `PROCEDURES_TEXT_ENABLED=1`.

---

## Known boundaries (by design)

- **Cross-aerodrome enumeration** ("which aerodromes use 5000 ft TA") is declined
  in favour of per-aerodrome lookup — the data isn't uniformly extractable, and a
  silently-incomplete list is unsafe.
- **Live weather / METAR / TAF values, NOTAMs, flight planning, current
  runway-in-use, ATC clearances, and any price/fee are out of scope by
  design**, not by omission — `agent._SYSTEM`'s out-of-scope test is TIME
  (something happening now) / MONEY (a price) / PLACE (outside Nigeria); any
  one true forces a refusal even where the AIP would otherwise seem to answer.
  A prior version tried to enumerate in-scope topics instead, which is
  unbounded (a pilot can always phrase around a keyword list) — TIME/MONEY/
  PLACE is the small, closed set instead.
- **AIRAC currency has no automated gate yet** — see *AIRAC currency* above.
  This is the most significant open safety item.
- **Data correctness & currency** rest on human validation and the AIRAC pipeline,
  not on the code alone.
- **A small number of AD 2.x fields are confirmed genuine source gaps**, not
  extraction bugs — e.g. two aerodromes' declared distances, one aerodrome's
  comms frequency, three aerodromes' navaid records. Each was individually
  confirmed against the real PDF page before being accepted as a gap rather than
  a bug. Full list in `PROJECT_SUMMARY.md`.
- **A handful of confirmed source typos in the AIP itself** are corrected in
  code, always flagged in extractor warnings and checked against a second
  section that publishes the same value rather than trusted outright: ENR
  3.3's GANDA longitude (`003100.60E` → `0031000.60E`, confirmed against
  ENR 4.4 and by NAMA directly). ENR 3.2's TEGDA shows a 30 arc-second
  discrepancy against ENR 4.4 that is flagged but **not yet corrected** —
  awaiting confirmation of which value is right.

---

*Layer 2 extraction architecture and design rationale: see `PROJECT_SUMMARY.md`.*
*AIRAC cycle upgrade process (structural diffing, expected-count regression,
cross-section checks, the five-gate release process): see
`AIRAC_UPGRADE_FRAMEWORK.md`.*
