# Vannie — Nigeria AIP Reference Assistant

Hardened rewrite of the Telegram AIP bot. Layered, fail-safe, extractive, and
explicitly a **reference aid — not an operational source**.

## Module map

| File | Role |
|---|---|
| `config.py` | Settings, models, thresholds, AIRAC cycle, safety copy |
| `models.py` | Internal dataclasses (Resolution, AIPResult, SearchOutcome, ChartRef) |
| `schemas.py` | Strict LLM extraction schema — **no ICAO guessing** |
| `agent.py` | The only LLM use: structured extraction + embeddings (both cheap, both wrapped) |
| `resolver.py` | **Deterministic ICAO/part resolution from your own data** |
| `database.py` | Supabase RPCs with fallback + **max-similarity gating** + chart media typing |
| `responder.py` | Extractive replies with citation + AIRAC + disclaimer; message splitting |
| `telegram.py` | Plain-text delivery, photo-vs-document charts, webhook secret check |
| `main.py` | Webhook: secret → dedup → fast 200 → background pipeline → error fallback |

## What changed vs. the original (the fixes from review)

- **Wrong-airport hallucination closed.** ICAO is resolved deterministically from
  `aip_charts` (+ a small verified alias list), not by the LLM. A name matching
  two airports asks the user; a name we don't have is refused. No silent wrong code.
- **Abstention gate corrected.** Confidence is judged on the *max* similarity across
  results, not `data[0]` (which was the top-*tier* row, not the most similar).
- **Hard-filter misclassification neutralised.** `aip_part`/`reference_tag` are tried
  across a small safe set of combinations; an aerodrome's reference stays pinned to
  its ICAO, so retrieval can't drift to another airport. "Not in the AIP" now means
  not found after fallback — not a bad guess by the classifier.
- **Currency + citation + disclaimer on every reply** (`AIRAC AMDT 03/2026`).
- **Telegram/webhook hardening:** 4096-char splitting, plain text (no Markdown
  breakage), PDF→`sendDocument` / image→`sendPhoto` with photo→document fallback,
  secret-token auth, fast 200 ack, `update_id` dedup, per-chat throttle, and a
  user-facing error fallback so nothing fails silently.

## Deploy

```bash
pip install -r requirements.txt
cp .env.example .env   # fill it in
uvicorn main:app --host 0.0.0.0 --port 8000
```

Register the webhook with the secret so auth is enforced:

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://YOUR_HOST/webhook" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

## One thing to confirm

`NATIONAL_REFERENCE_TAGS` in `.env` is a best guess. Run:

```sql
select distinct aip_part, reference_tag from aip_knowledge_base;
```

and set it to the real tags your national/en-route chunks use. (For aerodrome
queries this doesn't matter — reference is the ICAO.)

## Optional, sharper citations

`match_aip_text_advanced` currently returns only `content, chart_url, similarity`,
so citations are at the part/reference level. If you add `reference_tag` and a page
column to its `RETURNS TABLE`, the responder can cite per chunk (section + page).
Small RPC change, no app rewrite.
