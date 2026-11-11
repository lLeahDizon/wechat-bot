# Repository Instructions

## Source synchronization

- This private GitHub repository, `lLeahDizon/wechat-bot`, is the durable source copy for the maintained Sub2API WeChat bot.
- Any bot source-code change made directly on the running server must be brought back into this repository, verified, committed, and pushed to GitHub before the work is considered complete. Do not leave server-only code fixes.
- For planned work, make and verify changes in this repository before deployment. If an emergency requires a direct server edit, backport it here immediately afterward and verify the repository version matches the deployed fix.
- Record user-facing behavior changes in `CHANGELOG.md`.

## Data boundary

- Commit source, tests, documentation, and sanitized examples only.
- Never commit production configuration, credentials, API keys, member identifiers, chat content, server logs, rollback archives, or generated runtime evidence.
- Keep `config/policy.json` and all server-specific runtime data out of Git; use the sanitized example configuration instead.
