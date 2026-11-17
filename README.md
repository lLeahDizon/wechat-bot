# WeChat Bot

Source for an AstrBot-based WeChat group assistant connected to Sub2API.

The bot supports general question answering, public web research, League of Legends knowledge, read-only service diagnostics, reminders, usage summaries, and investment research. Runtime credentials, group bindings, and deployment evidence are supplied separately and are not part of this repository.

Run the offline test suite with:

```sh
python3 -m pip install -r plugin/requirements.txt
python3 -B -m unittest discover -s tests
```

Public research can extract text from PDFs up to the existing 2 MiB download limit, 40 pages, and 40,000 characters. Parsing stays within the per-page fetch deadline; encrypted, image-only, or otherwise unreadable PDFs remain unsupported, and truncated reports are labeled partial. PDF bytes are processed in memory and are not cached.

The Native WeChat adapter under `vendor/astrbot_plugin_wechat_native` is vendored from its upstream project; its license is included with the source. Use `config/policy.example.json` as a template. The runtime `config/policy.json` is ignored and must be supplied separately.

## Source synchronization

This private GitHub repository is the durable source copy. Server-side code fixes must be brought back here, verified, committed, and pushed; planned changes should be developed from this repository. See `AGENTS.md`. Record user-facing changes in `CHANGELOG.md`.

## Investment failure diagnostics

Investment model failures retain only a normalized error code and timestamp in the existing delivery metadata for up to seven days. From the deployed source directory, operators can run `python3 admin/model_failures.py --limit 20` to read them. The output omits message text, sender IDs, and credentials.

## Shared upstream account quota

True mentions asking `账号用量`, `账号剩余额度`, or `账号重置时间和剩余重置次数`
use a fixed read-only query. Existing `我的用量`, member aliases, Token/cost queries,
and rankings retain their separate personal-Key accounting. Ambiguous bare usage
questions are clarified. Ordinary account quota replies and a short development
recommendation use no model call; explicit detailed follow-ups use at most one
ordinary model call and retain the locally verified quota figures.

The dedicated `bot_diagnostics.account_quota` view compares the active OAuth
records' identity internally without exposing it. The reader requires one verified
shared identity, chooses the latest valid window snapshot, and never sums or
averages duplicate account records. Missing/invalid windows remain unknown;
expired data is not presented as a new zero-use window. `codex_usage_updated_at`
is the stored account snapshot timestamp, not a per-window timestamp or the time
the bot query ran. Samples over five minutes old are labeled as previous records.
Percentages have two decimals; reset, sample, and expiry times use Beijing time
to the minute.

Reset opportunities are separate from the natural quota window reset. Their
stored snapshot has no independent sampling timestamp. Conflicting, missing,
incomplete, or expired records do not yield a current count. Matching records are
explicitly labeled as cached, possibly changed, with each opportunity's expiry;
expiry is not a replenishment date. Quota sampling times cannot date that cache.

The reader never calls usage/quota management APIs, probes a model, refreshes
credentials, consumes reset opportunities, or changes account mappings. Only
the existing read-only role gains SELECT on this view; it gains no access to
account/credential base tables. Apply `admin.account_quota_view.view_sql()` only
through the scoped rollout after backing up the previous task-owned view and
runtime files. Load the changed query service and AstrBot modules once; preserve
Native login, inbox/cursor, member bindings, and uncertain-send records.

`admin.account_quota_rollout` applies a hash-checked patch to a fixed source-file
allowlist, preserving server-specific text and configuration. Its stages are
`prepare --patch-sha256 HASH` (patch on stdin), `verify-prepared`, `activate`, and
`verify-live`. Run them from the deployed source directory through the operator
route. Preparation saves the previous files and view definition in the existing
restricted rollback/evidence locations. Verification uses a network-disabled
framework fixture and the read-only database role. Activation records each
restart attempt before issuing it; an interrupted or uncertain activation must
be reconciled from its receipt rather than rerun.
