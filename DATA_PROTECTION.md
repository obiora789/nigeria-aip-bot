# Vannie — data protection record

Record of processing for Vannie, the Nigerian AIP Telegram assistant, under the Nigeria Data Protection Act 2023 (NDPA) and the NDPC's General Application and Implementation Directive 2025 (GAID). It is written for an auditor and kept with the code, so it changes when the code does.

**Status:** first version, 27 October 2026. This is an engineering record, not legal advice. The lawful bases and the cross-border transfer basis below need confirming by a Nigerian data-protection lawyer or a licensed Data Protection Compliance Organisation (DPCO). Items marked **[INFORMATION NEEDED]** must be filled in before it is relied on.

## 1. Controller

| | |
|---|---|
| Controller | [INFORMATION NEEDED — Obiora Okafor / KODVEX, as registered] |
| Contact for data requests | `PRIVACY_CONTACT` setting (shown in `/privacy`) [INFORMATION NEEDED — set it] |
| Data Protection Officer | [INFORMATION NEEDED — required for a controller of major importance] |
| NDPC registration | [INFORMATION NEEDED — check whether Vannie is a controller of major importance; register if so] |

## 2. What is processed, and why

| Data | Where it comes from | Purpose | Lawful basis (to confirm) | Kept for |
|---|---|---|---|---|
| Question text | The pilot's Telegram message | Answering it; finding wrong answers | Legitimate interests (safety and quality of an aviation reference); a Legitimate Interest Assessment is needed (GAID template) | `LOG_RETENTION_DAYS` (default 180), then deleted automatically |
| Vannie's reply text | Vannie | Explaining and reproducing any answer later (audit trail) | As above | As above |
| Pseudonymous chat key (`chat_hash`) | SHA-256 of the Telegram chat ID combined with the secret `CHAT_HASH_PEPPER`; the ID itself is never stored | Linking a pilot's rows for `/deletemydata` and for follow-ups | As above | As above |
| Aerodrome, AIP section, route taken, similarity, chart count | Vannie | Quality review | As above | As above |
| 👍/👎 feedback | The pilot | Review queue | As above | As above |
| Model names, prompt fingerprint, app version, AIRAC cycle | Vannie | Audit trail | Not personal data | As above |
| Conversation context (last aerodrome, pending choice) | Vannie | Follow-up questions ("and the TORA?") | As above | `CONTEXT_TTL_MIN` (default 10 minutes); expired rows deleted daily |

Not collected: names, phone numbers, Telegram usernames, location, payment data. **Planned accounts and subscriptions will add personal and payment data. This record must be updated, and a DPIA done, before they launch.**

Free text: a pilot can type anything, including personal details, into a question. That text is stored as typed until the retention period ends or the pilot uses `/deletemydata`.

## 3. Who else processes it (processors)

| Processor | What it receives | Location | Terms |
|---|---|---|---|
| OpenAI (API) | Question text (extraction, double-meaning check, embedding); retrieved AIP text and the question (synthesis). No Telegram ID. | USA (default; regional processing is available on request) | API data not used for training by default; kept up to 30 days for abuse monitoring; Zero Data Retention available on approval. Source: developers.openai.com/api/docs/guides/your-data. Data Processing Addendum: [INFORMATION NEEDED — confirm accepted] |
| Supabase | Query log, conversation context, AIP data | [INFORMATION NEEDED — project region] | [INFORMATION NEEDED — DPA] |
| Render | Runs the service; application logs | [INFORMATION NEEDED — service region] | [INFORMATION NEEDED — DPA] |
| Telegram | All messages, as the messaging platform | Telegram's infrastructure | Telegram's own terms; the pilot's relationship with Telegram |
| Redis (if `REDIS_URL` set) | Telegram update IDs and chat IDs, held for seconds to an hour (dedup, throttle) | [INFORMATION NEEDED] | [INFORMATION NEEDED] |

## 4. Transfers outside Nigeria

Question text goes to OpenAI in the USA, and possibly to Supabase and Render outside Nigeria. The NDPA (Part VIII) allows a transfer when the recipient is subject to adequate protection, or on another basis the Act provides. The GAID requires Commission approval for transfers relying on binding corporate rules or standard contractual clauses.

**Basis relied on: [INFORMATION NEEDED — to be decided with counsel].** Options to discuss:

- an approved transfer instrument with each processor;
- the pilot's informed consent after being told of the transfer — `/privacy` already tells them, and consent at first use could be added;
- OpenAI regional processing or Zero Data Retention, to reduce what leaves Nigeria and how long it is kept.

## 5. Pilots' rights and how each is met

| Right | How |
|---|---|
| Be informed | `/privacy` (text in `config.privacy_notice()`); greeting and `/help` say Vannie is an AI system run on OpenAI models |
| Erasure | `/deletemydata` deletes every query-log row keyed to the chat and the conversation context, at once. The request itself is not logged. |
| Access, portability | On request to the contact: rows filtered by the chat key, exported with `observability.export_csv` |
| Rectification | On request (a stored question is a record of what was asked; corrections are noted, not rewritten) |
| Object / complain | On request; pilots are told they can complain to the NDPC |

## 6. Security measures

- **Webhook:** refuses every update unless Telegram's secret token matches `TELEGRAM_WEBHOOK_SECRET` (constant-time comparison); fails closed when unset.
- **Dashboard:** token in an HttpOnly cookie, constant-time comparison, CSRF guard on changes, web prune floored at 7 days, full wipe CLI-only.
- **Pseudonymisation:** chat keys use a secret pepper for both the query log and conversation context.
- **Retention:** automatic daily deletion (`LOG_RETENTION_DAYS`, expired context).
- **Dependencies:** pinned exact versions (`requirements.txt` from `requirements.in`); `pip-audit` blocks deploys on known vulnerabilities; Dependabot weekly.
- **Code changes:** CI runs every test before a deploy is triggered.
- **Open items:** Supabase row-level security; who holds the Supabase service key and how it is rotated; Render log contents. [INFORMATION NEEDED]

## 7. Breaches

Notify the NDPC within 72 hours of becoming aware of a breach likely to risk pilots' rights, and tell affected pilots without undue delay where the risk is high (NDPA; confirm the current procedure with counsel). Breach log: [INFORMATION NEEDED — where it is kept].

## 8. Audit trail of answers

Each logged question records:

- the reply sent;
- the models (pinned snapshots);
- `prompt_version`, a fingerprint of every prompt and output schema;
- `app_version`, the deployed commit;
- the AIRAC cycle.

Together these let any answer be explained and reproduced while the row is retained.

## 9. Review

Review this record when any of the following changes: data collected, a processor, retention, the model provider or model, or the launch of accounts or payments. Last reviewed: 27 October 2026.
