# Changelog

User-facing bot changes are recorded here in reverse chronological order. Dates use `YYYY-MM-DD`.

## 2026-11-20

- Added passive, aggregated risk queries for listener exposure, UFW, SSH authentication events, declared service versions and task backup metadata, with explicit evidence limitations.
- Added bounded historical host metric queries and explicit time ranges for request/event/SSH evidence; raw logs and infrastructure identities stay private.
- Expanded diagnostic planning to ten queries and four model calls within an isolated 60-second deadline, preserving all other workflow budgets and the group mention/read-only boundary.
- Raised shared question-answering, research, suggestion review, weekly summaries and public search reasoning to high, subject to effective server model mapping.

## 2026-11-19

- Simplified Token usage replies to show only each member's total, while preserving automatic units, ranking context, and separate fee rankings.

## 2026-11-18

- Token and cost ranking requests now return both leaderboards for the same Beijing-time period. The default is today; explicit dates, ranges, and all retained history remain available.
- Repeating a usage question in a new message runs a fresh query. Duplicate delivery of the same message is still ignored.

## 2026-11-17

- Split a previously shared member-usage binding into independent Key statistics, added a public member alias, and removed the old shared ranking label. Personal queries and historical ranges use each member's newly bound Key only.
- Added anonymous shared-account quota queries, distinct from personal Key Token/cost usage and rankings. Duplicate authorizations share one latest valid quota snapshot; invalid, expired, or stale windows are labeled explicitly.
- Added a local development-use recommendation, with one optional model call for detailed follow-ups. Suggestions account for server mapping and never invent per-model quota savings or remaining request counts.
- Show natural reset/sample times and cached reset-opportunity expiry times in Beijing time to the minute. Reset opportunities use the first created active authorization, with caches lacking sampling time labeled accordingly; other authorizations do not override that count.
- Include spendable Codex points from the same authorization, with two decimals and a separate sampling time. Missing, zero, unlimited, and stale snapshots remain distinct. Queries never probe models, refresh credentials, or perform resets.
- Condensed account quota replies by combining sampling/reset times, reset opportunities and expiry times, and the point snapshot; all times share one Beijing-time heading, with the year retained for cross-year dates.

## 2026-11-16

- Added an optional isolated IP model routing proxy for the Sub2API gateway, including HTTP/SSE and Responses WebSocket support, unchanged API-key attribution, deterministic tests, deployment guidance, and rollback steps.

## 2026-11-13

- Updated private member bindings and aliases for an existing shared usage bucket; rankings label the shared bucket and count its usage once.

## 2026-11-11

- Added an AI Berkshire-inspired investment research flow with 22 fixed research entry points for A-shares, Hong Kong, U.S., and Taiwan stocks, using verifiable public sources with data timestamps.
- Added source cross-checking, explicit missing-data handling, and per-question limits of six model calls, ten public pages, and 90 seconds. Technical indicators or backtests run only when verifiable public daily data is available.
- Limited personalized position analysis to information explicitly shared by the asking member in the current question or relevant group history; the bot does not persist investment profiles.
- Answered stock-research capability questions locally instead of sending them through company research.
- Resolved company names without requiring a security code, searched across supported markets when needed, prioritized matching public sources, and avoided exposing an internal unknown-market marker.
- Added bounded text extraction for public PDF reports so verified financial figures can reach the existing research answer and cross-check table; partial documents are labeled accordingly.
- Increased the per-page public research fetch allowance to eight seconds while preserving the 90-second per-question deadline.
- Added seven-day retention of normalized investment failure codes for operator diagnosis, without storing chat text or credentials.
