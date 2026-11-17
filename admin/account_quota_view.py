"""Create only the anonymous quota view using the existing read-only role."""

from .provision_diagnostics import MARKER

VIEW_MARKER = MARKER + "-account-quota-v1"


def _number(key, integer=False):
    value = "a.extra->>'" + key + "'"
    regex = "^[0-9]{1,8}$" if integer else "^[0-9]{1,3}(\\.[0-9]{1,8})?$"
    cast = "integer" if integer else "numeric"
    return f"CASE WHEN {value} ~ '{regex}' THEN ({value})::{cast} END"


def _stamp(key):
    value = "a.extra->>'" + key + "'"
    regex = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$"
    return f"CASE WHEN {value} ~ '{regex}' THEN {value} END"


def view_sql():
    columns = [_stamp("codex_usage_updated_at") + " AS sampled_at"]
    for prefix, upstream in [("short", "5h"), ("weekly", "7d")]:
        columns.extend(
            [
                _number(f"codex_{upstream}_used_percent")
                + f" AS {prefix}_used_percent",
                _number(f"codex_{upstream}_window_minutes", True)
                + f" AS {prefix}_window_minutes",
                _stamp(f"codex_{upstream}_reset_at") + f" AS {prefix}_reset_at",
            ]
        )
    snapshot = "a.extra->'codex_reset_credit_snapshot'"
    count = f"{snapshot}->>'available_count'"
    credits = f"{snapshot}->'credits'"
    expiry = "credit->>'expires_at'"
    stamp_regex = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$"
    columns.extend(
        [
            f"coalesce(jsonb_typeof({snapshot})='object',false) AS reset_credit_snapshot_present",
            f"CASE WHEN {count} ~ '^[0-9]{{1,2}}$' THEN ({count})::integer END AS reset_credit_count",
            f"CASE WHEN jsonb_typeof({credits})='array' THEN CASE WHEN jsonb_array_length({credits})<=32 THEN "
            f"(SELECT coalesce(jsonb_agg(CASE WHEN {expiry} ~ '{stamp_regex}' THEN {expiry} END),'[]'::jsonb) "
            f"FROM jsonb_array_elements({credits}) credit) END END AS reset_credit_expirations",
        ]
    )
    points = "a.extra->'codex_credits_snapshot'"
    details = f"{points}->'credits'"
    balance = f"btrim({details}->>'balance')"
    sampled = f"{points}->>'fetched_at'"
    columns.extend([
        "row_number() OVER (ORDER BY a.id) AS record_order",
        f"coalesce(jsonb_typeof({details})='object',false) AS points_present",
        f"CASE WHEN jsonb_typeof({details}->'has_credits')='boolean' THEN ({details}->>'has_credits')::boolean END AS points_available",
        f"CASE WHEN jsonb_typeof({details}->'unlimited')='boolean' THEN ({details}->>'unlimited')::boolean END AS points_unlimited",
        f"CASE WHEN jsonb_typeof({details}->'balance')='string' AND {balance} ~ '^[0-9]{{1,15}}(\\.[0-9]{{1,12}})?$' THEN {balance} END AS points_balance",
        f"CASE WHEN {sampled} ~ '^[0-9]{{1,12}}$' THEN ({sampled})::bigint END AS points_sampled_at",
    ])
    projection = ",\n ".join(columns)
    return f"""BEGIN;
SET LOCAL lock_timeout='1s'; SET LOCAL statement_timeout='5s';
DO $guard$ BEGIN
 IF (SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname='bot_diagnostics') IS DISTINCT FROM '{MARKER}'
 OR (SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname='bot_diagnostics_ro') IS DISTINCT FROM '{MARKER}' THEN
  RAISE EXCEPTION 'quota_role_or_schema_mismatch';
 END IF;
 IF to_regclass('bot_diagnostics.account_quota') IS NOT NULL AND
    obj_description(to_regclass('bot_diagnostics.account_quota'),'pg_class') IS DISTINCT FROM '{VIEW_MARKER}' THEN
  RAISE EXCEPTION 'quota_view_collision';
 END IF;
END $guard$;
CREATE OR REPLACE VIEW bot_diagnostics.account_quota WITH (security_barrier=true) AS
 WITH eligible AS (
  SELECT id,extra,
   CASE WHEN jsonb_typeof(credentials->'chatgpt_account_id')='string' THEN nullif(btrim(credentials->>'chatgpt_account_id'),'') END AS account_identity,
   CASE WHEN jsonb_typeof(credentials->'chatgpt_user_id')='string' THEN nullif(btrim(credentials->>'chatgpt_user_id'),'') END AS user_identity
  FROM public.accounts WHERE deleted_at IS NULL AND platform='openai' AND type='oauth' AND status='active' AND schedulable
 ), scope AS (
  SELECT count(*) AS eligible_records,
   count(DISTINCT (account_identity,user_identity)) FILTER(WHERE account_identity IS NOT NULL AND user_identity IS NOT NULL) AS identity_count,
   count(*) FILTER(WHERE account_identity IS NULL OR user_identity IS NULL) AS unidentified_records FROM eligible
 ) SELECT scope.eligible_records,scope.identity_count,scope.unidentified_records,
 {projection}
 FROM eligible a CROSS JOIN scope;
COMMENT ON VIEW bot_diagnostics.account_quota IS '{VIEW_MARKER}';
REVOKE ALL ON bot_diagnostics.account_quota FROM PUBLIC;
GRANT SELECT ON bot_diagnostics.account_quota TO bot_diagnostics_ro;
COMMIT;
"""
