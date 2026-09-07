"""Portal-safe governance projection for the monitoring domain."""

from __future__ import annotations

def governance_monitoring_snapshots(records, store) -> dict[str, dict]:
    """Return body-free, version-matched governance scan metadata."""
    scans = store.monitoring_latest_scans(
        [record.record_id for record in records]
    )
    snapshots: dict[str, dict] = {}
    for record in records:
        scan = scans.get(record.record_id)
        reason = None
        if scan is not None and not scan.version:
            reason = "scan_version_unknown"
        elif scan is not None and scan.version != record.version:
            reason = "scan_version_mismatch"
        current = scan if reason is None else None
        snapshots[record.record_id] = {
            "scan_status": current.status if current else None,
            "scan_risk": current.risk if current else None,
            "gate_verdict": None,
            "as_of": current.ts if current else None,
            "reason": reason,
        }
    return snapshots
