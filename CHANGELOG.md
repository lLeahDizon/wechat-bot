# Changelog

User-facing bot changes are recorded here in reverse chronological order. Dates use `YYYY-MM-DD`.

## 2026-11-17

- Added anonymous shared-account quota queries, distinct from personal Key Token/cost usage and rankings. Duplicate authorizations share one latest valid quota snapshot; invalid, expired, or stale windows are labeled explicitly.
- Added a local development-use recommendation, with one optional model call for detailed follow-ups. Suggestions account for server mapping and never invent per-model quota savings or remaining request counts.
- Show natural reset/sample times and cached reset-opportunity expiry times in Beijing time to the minute. Conflicting reset counts remain unknown, and caches without sampling time are labeled accordingly. Queries never probe models, refresh credentials, or perform resets.

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
