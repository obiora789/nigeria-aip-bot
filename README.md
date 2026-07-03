# Vannie — Nigeria AIP Reference Assistant

A Telegram bot that answers questions about the **Nigerian AIP** (currently
`AIRAC AMDT 03/2026`) — frequencies, runway data, declared distances, navaids,
charts, ICAO mapping — for pilots and dispatchers.

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
        → deterministic ICAO/section resolution (from our own data)
        → conversation context (slot-fill / follow-up, surfaced not silent)
        → route:
            greeting / help            → static copy
            structure question         → deterministic ToC map (toc.py)
            cross-aerodrome enumeration → structured-facts boundary (facts.py)
            ICAO ↔ city mapping         → deterministic table
            chart request              → plate(s) [+ scoped procedures text]
            everything else            → vector search (max-sim gated)
                                          → grounded synthesis + verifier
                                          → verbatim fallback / faithful abstain
        → log the query + outcome (observability) → 👍/👎 feedback buttons
```

## Module map

| File | Role |
|---|---|
| `config.py` | Settings, env vars, models, thresholds, AIRAC cycle, safety copy, feature toggles |
| `models.py` | Internal dataclasses (Resolution, AIPResult, SearchOutcome, ChartRef) |
| `schemas.py` | Strict LLM extraction schema (**no ICAO guessing**) + `GroundedAnswer` |
| `agent.py` | The LLM boundary: structured extraction + embeddings, plus deterministic backstops (fabricated-ICAO drop, chart-intent force, MET/comms reroute) |
| `resolver.py` | **Deterministic ICAO/section resolution** from our own data; aliases, verified VOR idents, per-field query enrichment, `build_search_text` |
| `database.py` | Vector search with **max-similarity gating**; `get_charts_smart` (direct table, synonym + side-aware runway match); `get_section_text` |
| `synthesize.py` | **Grounded synthesis + deterministic verifier** (every asserted number must be in source or a shown computation); **minima carve-out** (never synthesizes a decision height) |
| `responder.py` | Extractive replies with citation + AIRAC + disclaimer; focused single-chunk source blocks; **side-aware runway matching** (18L ≠ 18R) |
| `procedures.py` | **Verbatim approach-procedure sectioniser** — scopes to the exact approach, splits Holding/Letdown/Missed, all-three-or-plate fallback (toggle-gated) |
| `toc.py` | Deterministic AIP table-of-contents for "which part covers X" |
| `facts.py` | Cross-aerodrome enumeration handled as a documented boundary |
| `telegram.py` | Delivery, photo-vs-document charts, 👍/👎 keyboard, callback ack, webhook secret |
| `memory.py` | Short-term conversation context (slot-fill + follow-up carry), TTL-bounded |
| `cache.py` | Redis-backed dedup/throttle with in-memory fallback (optional) |
| `observability.py` | Query log, startup/deep healthcheck, `/dashboard` renderer, triage helpers |
| `alerting.py` | Throttled operator alerts on health degrade + recovery notices |
| `main.py` | Webhook + pipeline + `/health`, `/health/deep`, `/dashboard`; background health monitor |
| `triage.py` | CLI weekly-review tool for the query log (`--open`, `--mark`, `--icao`) |
| `vectorise_aip_v2.py` | **Ingestion**: column-aware PDF extraction, subsection-aware AD 2 chunking, chart-page exclusion → `aip_knowledge_base` |
| `eval_set.py` / `e2e.py` / `harness.py` / `test_offline.py` | Evaluation + offline tests |

`extract_charts.py` (run separately, before the vectoriser) populates the
`aip_charts` catalogue used for chart retrieval and plate-page exclusion.

---

## Safety design (the guardrails)

- **No wrong-airport hallucination.** ICAO is resolved deterministically from our
  own data + a verified alias list, never guessed by the LLM. Ambiguous name →
  ask; unknown name → refuse.
- **Grounded synthesis with a verifier.** The model may compute and compare, but
  only over retrieved AIP text; every number it asserts must appear in the source
  or be the result of arithmetic it shows. Any ungrounded number → reject → fall
  back to verbatim. Fails safe.
- **Verbatim for the highest-stakes values.** Approach minima / decision heights
  are never synthesized — the table is shown as-is (`synthesize._MINIMA_RE`).
- **Approach procedures are scoped, not spliced.** `procedures.py` only shows
  Holding/Letdown/Missed when it can pin them to one approach and all three parse
  cleanly; otherwise it defers to the plate. The plate always follows anyway.
- **Faithful abstention.** "Not in the AIP" means not found after fallback, and
  is shown as an honest refusal — not a bad guess.
- **Currency + citation + disclaimer on every reply.**

---

## Data layer

Two Supabase tables back the bot:

- `aip_knowledge_base` — embedded AIP text chunks (`text-embedding-3-small`, 1536-dim).
  Built by `vectorise_aip_v2.py`, which now reads pages **column-aware** (two-column
  AIP pages are read in correct order, not flattened) and chunks AD 2 by subsection
  (`AD 2.NN`) so each field's header and values stay together.
- `aip_charts` — the chart/plate catalogue (built by `extract_charts.py`).

**Re-ingesting** (after any extraction/chunking change, or a new AIRAC):

```sql
DELETE FROM aip_knowledge_base;
```
```bash
rm vectorise_v2_progress.json
python vectorise_aip_v2.py      # extract_charts.py must have populated aip_charts first
```

---

## Observability & feedback

- Every query writes a row to `vannie_query_log` (query, intent, ICAO, path,
  similarity, charts). Non-confident outcomes auto-flag `needs_review`.
- **Feedback:** 👍/👎 buttons under substantive answers; a 👎 flags that query for
  review — real wrong answers surface themselves.
- **Triage:** `python triage.py` (`--days`, `--icao`, `--open`, `--all`,
  `--mark <id> --note "…"`). Mark items handled so the queue converges.
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
+ `/health/deep`), `ADMIN_CHAT_ID` (your Telegram id, for alerts).

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
```

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

---

## AIRAC currency

The corpus is a single AIRAC cycle. AIP data changes on a 28-day cycle, and a
reference tool serving stale data is worse than none. Each cycle: re-run
`extract_charts.py` → re-vectorise → **validate a sample against the new AIP** →
update `AIRAC_CYCLE`. This validation + currency pipeline is human-owned and is
the most important ongoing safety work.

---

## Approach-procedure text (validate before enabling)

`PROCEDURES_TEXT_ENABLED` is **off by default**. When off, approach requests get
the plate (with a pointer). When on, Vannie also shows the verbatim
Holding/Letdown/Missed-Approach text, scoped to the requested approach.

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
- **Live weather / METAR / TAF values, NOTAMs, flight planning** are out of scope.
- **Data correctness & currency** rest on human validation and the AIRAC pipeline,
  not on the code alone.
