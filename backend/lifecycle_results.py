"""Contract and state-application boundary for complex existence verification results."""
from dataclasses import asdict, dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from typing import Any, Iterable, Sequence

from psycopg.types.json import Jsonb

VALID_EXISTENCE = ("present", "demolished", "pending")
VALID_PRECISION = ("day", "month", "year")


@dataclass(frozen=True)
class LifecycleResult:
    result_key: str
    complex_id: str
    existence: str
    evidence: str
    as_of: date | str
    verifier_version: str
    hold_code: str | None = None
    correction_of: str | None = None
    successor_id: str | None = None
    successor_status: str | None = None
    effective_date: date | str | None = None
    effective_precision: str | None = None


def empty_lifecycle_provider(
    district: str, as_of: date, complex_ids: Sequence[str]
) -> list[LifecycleResult]:
    """Default no-op provider used while the external lifecycle verifier is disconnected."""
    return []


def _parse_iso_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and len(value.strip()) == 10:
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _clean_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _normalize_record(raw: LifecycleResult | dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    data = asdict(raw) if isinstance(raw, LifecycleResult) else (dict(raw) if isinstance(raw, dict) else {"raw": str(raw)})
    serializable = json.loads(json.dumps(data, default=str))

    result_key = _clean_optional_str(data.get("result_key"))
    complex_id = _clean_optional_str(data.get("complex_id"))
    existence = _clean_optional_str(data.get("existence"))
    evidence = _clean_optional_str(data.get("evidence"))
    verifier_version = _clean_optional_str(data.get("verifier_version"))
    as_of_date = _parse_iso_date(data.get("as_of"))

    if not result_key or not complex_id or not verifier_version or as_of_date is None:
        return None, "INVALID_REQUIRED_FIELDS", serializable
    if existence not in VALID_EXISTENCE:
        return None, "INVALID_EXISTENCE", serializable

    hold_code = _clean_optional_str(data.get("hold_code"))
    if not evidence:
        if existence == "pending" and hold_code:
            evidence = f"hold:{hold_code}"
        else:
            return None, "MISSING_EVIDENCE", serializable

    correction_of = _clean_optional_str(data.get("correction_of"))
    successor_id = _clean_optional_str(data.get("successor_id"))
    successor_status = _clean_optional_str(data.get("successor_status"))

    raw_eff_date = data.get("effective_date")
    raw_eff_prec = data.get("effective_precision")
    if raw_eff_date is None and raw_eff_prec is None:
        eff_date = None
        eff_prec = None
    else:
        eff_date = _parse_iso_date(raw_eff_date)
        eff_prec = _clean_optional_str(raw_eff_prec)
        if eff_date is None or eff_date > as_of_date or eff_prec not in VALID_PRECISION:
            return None, "INVALID_EFFECTIVE_DATE", serializable

    canonical = {
        "result_key": result_key,
        "complex_id": complex_id,
        "existence": existence,
        "evidence": evidence,
        "as_of": as_of_date.isoformat(),
        "verifier_version": verifier_version,
        "hold_code": hold_code,
        "correction_of": correction_of,
        "successor_id": successor_id,
        "successor_status": successor_status,
        "effective_date": eff_date.isoformat() if eff_date else None,
        "effective_precision": eff_prec,
    }
    digest = sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    normalized = {
        **canonical,
        "as_of": as_of_date,
        "effective_date": eff_date,
        "payload_digest": digest,
    }
    return normalized, None, serializable


def _quarantine(db, result_key: str, complex_id: str, existence: str | None, reason: str, raw_payload: dict[str, Any]):
    db.execute("""
        INSERT INTO complex_lifecycle_quarantine(result_key, complex_id, existence, reason, raw_payload, payload_digest)
        SELECT v.*, encode(sha256(convert_to(jsonb_build_array(v.result_key, v.complex_id, v.reason, v.payload)::text, 'UTF8')), 'hex')
        FROM (VALUES (%s::text, %s::text, %s::text, %s::text, %s::jsonb))
            AS v(result_key, complex_id, existence, reason, payload)
        ON CONFLICT DO NOTHING
    """, (result_key or "", complex_id or "", existence, reason, Jsonb(raw_payload)))


def apply_lifecycle_results(
    db, results: Iterable[LifecycleResult | dict[str, Any]], *, allowed_ids=None, cutoff=None,
) -> dict[str, int]:
    """Apply verified existence results atomically per record, isolating invalid or conflicting items."""
    summary = {"applied": 0, "noop": 0, "pending": 0, "quarantined": 0}
    for item in results or ():
        with db.transaction():
            rec, err_reason, raw_payload = _normalize_record(item)
            raw_key = str(raw_payload.get("result_key") or "").strip()
            raw_cid = str(raw_payload.get("complex_id") or "").strip()
            raw_ext = _clean_optional_str(raw_payload.get("existence"))
            if err_reason is not None or rec is None:
                _quarantine(db, raw_key, raw_cid, raw_ext, err_reason or "INVALID_PAYLOAD", raw_payload)
                summary["quarantined"] += 1
                continue

            result_key = rec["result_key"]
            complex_id = rec["complex_id"]
            existence = rec["existence"]
            as_of = rec["as_of"]
            digest = rec["payload_digest"]
            if allowed_ids is not None and complex_id not in allowed_ids:
                _quarantine(db, result_key, complex_id, existence, "OUTSIDE_REQUEST_SCOPE", raw_payload)
                summary["quarantined"] += 1
                continue
            if cutoff is not None and as_of > cutoff:
                _quarantine(db, result_key, complex_id, existence, "FUTURE_AS_OF", raw_payload)
                summary["quarantined"] += 1
                continue

            # Serialize retries of the same result key before testing for a prior application.
            db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (result_key,))
            prior = db.execute("""
                SELECT payload_digest FROM complex_lifecycle_results WHERE result_key = %s
            """, (result_key,)).fetchone()
            if prior is not None:
                prior_digest = prior[0] if isinstance(prior, tuple) else prior["payload_digest"]
                if prior_digest == digest:
                    summary["noop"] += 1
                else:
                    _quarantine(db, result_key, complex_id, existence, "DUPLICATE_KEY_CONFLICT", raw_payload)
                    summary["quarantined"] += 1
                continue

            exists_row = db.execute("""
                SELECT 1 FROM complexes WHERE complex_id = %s FOR UPDATE
            """, (complex_id,)).fetchone()
            if not exists_row:
                _quarantine(db, result_key, complex_id, existence, "UNKNOWN_COMPLEX_ID", raw_payload)
                summary["quarantined"] += 1
                continue

            state_row = db.execute("""
                SELECT result_key, existence, as_of, pending_as_of
                FROM complex_lifecycle_state
                WHERE complex_id = %s
                FOR UPDATE
            """, (complex_id,)).fetchone()
            if state_row is not None:
                if isinstance(state_row, tuple):
                    cur_key, cur_existence, cur_as_of, cur_pending_as_of = state_row
                else:
                    cur_key = state_row["result_key"]
                    cur_existence = state_row["existence"]
                    cur_as_of = state_row["as_of"]
                    cur_pending_as_of = state_row["pending_as_of"]
            else:
                cur_key, cur_existence, cur_as_of, cur_pending_as_of = (None, None, None, None)

            if existence == "pending":
                db.execute("""
                    INSERT INTO complex_lifecycle_results(
                        result_key, complex_id, existence, as_of, verifier_version, evidence,
                        payload_digest, hold_code, correction_of, successor_id, successor_status,
                        effective_date, effective_precision, applied_status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_recorded')
                """, (
                    result_key, complex_id, existence, as_of, rec["verifier_version"], rec["evidence"],
                    digest, rec["hold_code"], rec["correction_of"], rec["successor_id"], rec["successor_status"],
                    rec["effective_date"], rec["effective_precision"],
                ))
                if cur_pending_as_of is None or as_of >= cur_pending_as_of:
                    db.execute("""
                        INSERT INTO complex_lifecycle_state(
                            complex_id, pending_result_key, pending_hold_code, pending_as_of, updated_at
                        ) VALUES (%s, %s, %s, %s, clock_timestamp())
                        ON CONFLICT (complex_id) DO UPDATE
                        SET pending_result_key = EXCLUDED.pending_result_key,
                            pending_hold_code = EXCLUDED.pending_hold_code,
                            pending_as_of = EXCLUDED.pending_as_of,
                            updated_at = clock_timestamp()
                    """, (complex_id, result_key, rec["hold_code"], as_of))
                summary["pending"] += 1
                continue

            # Confirmed judgment ('present' or 'demolished')
            correction_of = rec["correction_of"]
            if correction_of is not None and correction_of != cur_key:
                _quarantine(db, result_key, complex_id, existence, "INVALID_CORRECTION_TARGET", raw_payload)
                summary["quarantined"] += 1
                continue

            if cur_existence is not None and cur_as_of is not None:
                if as_of < cur_as_of:
                    _quarantine(db, result_key, complex_id, existence, "STALE_AS_OF", raw_payload)
                    summary["quarantined"] += 1
                    continue
                if cur_existence == "demolished" and existence == "present" and correction_of != cur_key:
                    _quarantine(db, result_key, complex_id, existence, "DEMOLISHED_CONFLICT_WITHOUT_CORRECTION", raw_payload)
                    summary["quarantined"] += 1
                    continue
                if as_of == cur_as_of and cur_existence != existence and correction_of != cur_key:
                    _quarantine(db, result_key, complex_id, existence, "SAME_AS_OF_CONFLICT", raw_payload)
                    summary["quarantined"] += 1
                    continue

            db.execute("""
                INSERT INTO complex_lifecycle_results(
                    result_key, complex_id, existence, as_of, verifier_version, evidence,
                    payload_digest, hold_code, correction_of, successor_id, successor_status,
                    effective_date, effective_precision, applied_status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'applied')
            """, (
                result_key, complex_id, existence, as_of, rec["verifier_version"], rec["evidence"],
                digest, rec["hold_code"], correction_of, rec["successor_id"], rec["successor_status"],
                rec["effective_date"], rec["effective_precision"],
            ))
            db.execute("""
                INSERT INTO complex_lifecycle_state(
                    complex_id, result_key, existence, as_of, verifier_version, evidence,
                    effective_date, effective_precision, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())
                ON CONFLICT (complex_id) DO UPDATE
                SET result_key = EXCLUDED.result_key,
                    existence = EXCLUDED.existence,
                    as_of = EXCLUDED.as_of,
                    verifier_version = EXCLUDED.verifier_version,
                    evidence = EXCLUDED.evidence,
                    effective_date = EXCLUDED.effective_date,
                    effective_precision = EXCLUDED.effective_precision,
                    updated_at = clock_timestamp()
            """, (
                complex_id, result_key, existence, as_of, rec["verifier_version"],
                rec["evidence"], rec["effective_date"], rec["effective_precision"],
            ))
            summary["applied"] += 1
    return summary
