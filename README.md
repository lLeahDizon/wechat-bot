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

## Passive risk diagnostics

True mentions may request passive host/Sub2API risk checks. Fixed readers expose anonymous
listener counts, UFW policy/rule counts, bounded SSH authentication event counts, declared
container version tags, task-owned archive metadata, and retained host metric summaries.
No model-supplied shell, SQL, path or network target is accepted. Raw log lines, addresses,
ports, account identifiers, registry names and archive names do not leave the readers.
An SSH failure is a signal, not proof of intrusion; local listeners do not prove public
reachability, UFW does not cover cloud security groups, version tags do not verify binaries
or vulnerability applicability, and archives do not prove backup coverage or recoverability.
There is no active scan or additional automatic risk alert.

Diagnostics default to 15 minutes. `traffic`, `events`, `ssh`, and `history` accept integer
`range_start_epoch`/`range_end_epoch` for retained historical data. Current gateway logs
are limited to an 8 MiB tail, SSH journal output to 5,000 rows/1 MiB, and metric history
to 1,000 samples. Coverage, truncation and unavailable data remain explicit. Database
reads retain the read-only role, transaction and statement timeout; time ranges do not
grant access to additional tables. Instantaneous host/service readers cannot reconstruct
past state. Existing member usage date ranges and accounting remain unchanged.

The production diagnostic entry uses a disposable worker with a hard 60-second parent
timeout, at most ten fixed queries and four model calls, including follow-up planning.
Other workflow budgets stay independent. Shared Responses requests and public search
requests use `gpt-6.1-sol` with `high` reasoning; server-side mapping still selects the
effective upstream model. No paid synthetic probe is required for deployment: payload
contracts and existing metered high-effort records establish compatibility evidence;
actual group acceptance is recorded separately. A killed in-flight request may still be
charged upstream, and its final usage is unavailable to this worker.

Implementation checkpoint `botReadonlyRisk20261120`: source delivery target is `main`
in the original `work/wechat-bot` checkout; isolated work is
`feature/botReadonlyRiskSession1_20261120`, based on `720971a`.
Checklist: (1) baseline/isolation complete; (2) fixed readers and limits complete;
(3) model/controller and compatibility verification complete; (4) verification and
review complete, source push pending; (5) deployment and runtime acceptance pending.
Current repository suite: 401 passed. The server's older suite ran 339 tests after
the patch versus 329 from the backup, with the same five pre-existing failures in
legacy ranking/unit expectations and no added failures. This comparison does not
turn those stale server tests into passing tests. Ruff correctness checks and the
task diff's sensitive-information scan passed; browser E2E is not applicable.
Source commit/push authorization: `automatic_source_policy`, with `main` as the
explicit user-selected delivery target. Deployment verification and true-mention
acceptance are separate from source delivery.
Preserve Native login/cursor/history/bindings and uncertain-send state when loading changes.

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

Reset opportunities and spendable Codex points use the first created active
authorization (record ID ascending, ranked internally without exposing IDs).
Other authorization caches do not override this choice, even when newer or
different. The reset count has no independent sampling timestamp and is labeled
as cached, possibly changed, with each opportunity's expiry; expiry is not a
replenishment date. Missing, incomplete, or expired data in the selected record
remains unknown and does not fall back to another authorization.

Points are shown separately from reset opportunities and personal Key costs,
with two decimals and their own Beijing sampling time. A stale point snapshot is
labeled as a previous record; missing data is not reported as zero, and unlimited
points are explicit. Quota sampling times cannot date either of these caches.
Replies combine each value with its timestamp, use one Beijing-time heading,
and shorten same-year dates to month/day while retaining years across year
boundaries. Cached or unavailable data remains explicit.

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
The bounded `--revision primary-reset-points` rollout uses a separate receipt
and rollback snapshot for the primary-record selection and points extension.
`--revision concise-replies` loads the presentation change with an AstrBot-only
restart and verifies that the query service's process invocation is unchanged.
