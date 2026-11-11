# Changelog

User-facing bot changes are recorded here in reverse chronological order. Dates use `YYYY-MM-DD`.

## 2026-11-11

- Added an AI Berkshire-inspired investment research flow with 22 fixed research entry points for A-shares, Hong Kong, U.S., and Taiwan stocks, using verifiable public sources with data timestamps.
- Added source cross-checking, explicit missing-data handling, and per-question limits of six model calls, ten public pages, and 90 seconds. Technical indicators or backtests run only when verifiable public daily data is available.
- Limited personalized position analysis to information explicitly shared by the asking member in the current question or relevant group history; the bot does not persist investment profiles.
- Answered stock-research capability questions locally instead of sending them through company research.
- Resolved company names without requiring a security code, searched across supported markets when needed, prioritized matching public sources, and avoided exposing an internal unknown-market marker.
- Added bounded text extraction for public PDF reports so verified financial figures can reach the existing research answer and cross-check table; partial documents are labeled accordingly.
- Added seven-day retention of normalized investment failure codes for operator diagnosis, without storing chat text or credentials.
