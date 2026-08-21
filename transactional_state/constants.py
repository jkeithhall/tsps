"""Shared constants for the Transactional Shared Program State (TSPS)."""

# Rejection reasons (mirrors transaction_metrics.rejection_reason).
REASON_LOCK_CONTENTION = "lock_contention"
REASON_ASSERTION_VIOLATION = "assertion_violation"
REASON_SENSOR_FAIL = "sensor_fail"

# Committer recorded for files seeded from the repository base state.
BASE_COMMITTER = "BASE"

# Widen-on-Ambiguity event kinds (mirrors transaction_metrics.widening_kind).
WIDEN_STAR_IMPORT = "star_import"
WIDEN_GETATTR = "getattr"
WIDEN_ATTR_LOOP = "attr_loop"
WIDEN_DYNAMIC_WRITE = "dynamic_write"
