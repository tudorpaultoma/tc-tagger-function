"""
CBS Snapshot Tagging Service

Handles tagging for:
- CBS snapshots (CreateSnapshot)

Snapshots are point-in-time copies of CBS disks. They fire a CloudAudit event
under ResourceType "cbs" (captured by the existing tagger-cbs-track wildcard).

CloudAudit quirks:
- resourceId may arrive as a stringified Python list: "['snap-xxx']"
- eventSource / eventRegion may point to a different region than where the
  snapshot actually lives
- Region discovery: tries detected region first, then COS_REGION fallback

QCS format: qcs::cvm:{region}:uin/{uin}:snapshot/{snap_id}
  (Snapshot uses 'cvm' service namespace in CAM/Tag, NOT 'cbs')
"""

import json
import os
import datetime
from typing import List, Dict, Any, Optional


def build_snapshot_tags(owner: str, disk_id: str = "", disk_usage: str = "") -> List[Dict[str, str]]:
    """
    Build tags for snapshot resources.

    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCreated → TaggerDiskUsage → TaggerOwner →
    TaggerProject → TaggerSourceDisk → TaggerTTL

    Args:
        owner: Owner email/username
        disk_id: Source disk ID the snapshot was created from
        disk_usage: Source disk usage type (SYSTEM_DISK / DATA_DISK)

    Returns:
        List of tags to apply to snapshot
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",      "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",    "TagValue": today},
        {"TagKey": "TaggerSourceDisk", "TagValue": disk_id or "unknown"},
        {"TagKey": "TaggerDiskUsage",  "TagValue": disk_usage.upper() if disk_usage else "unknown"},
        {"TagKey": "TaggerCanDelete",  "TagValue": "YES"},
        {"TagKey": "TaggerTTL",        "TagValue": "3"},
        {"TagKey": "TaggerProject",    "TagValue": "n/a"},
    ]


def get_snapshot_info(snapshot_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query snapshot details using DescribeSnapshots API.

    Returns:
        Dict with keys: SnapshotId, SnapshotName, DiskId, DiskUsage,
                        SnapshotState, Percent, DiskSize
        None if snapshot not found or error
    """
    from index import make_tc_client
    from tencentcloud.cbs.v20170312 import cbs_client, models as cbs_models

    client = make_tc_client("cbs", cbs_client.CbsClient, region)
    if not client:
        return None

    try:
        req = cbs_models.DescribeSnapshotsRequest()
        req.SnapshotIds = [snapshot_id]
        resp = client.DescribeSnapshots(req)

        snapshots = getattr(resp, "SnapshotSet", [])
        if not snapshots:
            return None

        snap = snapshots[0]
        return {
            "SnapshotId":    getattr(snap, "SnapshotId", ""),
            "SnapshotName":  getattr(snap, "SnapshotName", ""),
            "DiskId":        getattr(snap, "DiskId", ""),
            "DiskUsage":     getattr(snap, "DiskUsage", ""),
            "SnapshotState": getattr(snap, "SnapshotState", ""),
            "Percent":       getattr(snap, "Percent", 0),
            "DiskSize":      getattr(snap, "DiskSize", 0),
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_snapshot_info_failed",
            "snapshot_id": snapshot_id,
            "region": region,
            "message": str(e)
        }))
        return None


def handle_snapshot_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle snapshot tagging for CreateSnapshot events.

    Extraction strategy:
    1. Try resourceSet for snapshot ID and region
    2. Fallback to responseElements.SnapshotId
    3. Query snapshot details for source disk info
    4. Build tags and apply via Tag API

    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs

    event_name = rec.get("eventName", "")
    if event_name != "CreateSnapshot":
        return False

    snap_id = None
    region = extract_region(rec)

    def _unwrap_id(val):
        """Extract a plain string ID from a value that may be a list or string."""
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.startswith("snap-"):
                    return item
            return val[0] if val and isinstance(val[0], str) else None
        if isinstance(val, str):
            if val.startswith("["):
                try:
                    parsed = json.loads(val.replace("'", '"'))
                    if isinstance(parsed, list) and parsed:
                        return parsed[0] if isinstance(parsed[0], str) else None
                except Exception:
                    pass
            return val if val else None
        return None

    # Try resourceSet first
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list):
        for resource in resource_set:
            if not isinstance(resource, dict):
                continue
            raw_id = resource.get("resourceId", "")
            unwrapped = _unwrap_id(raw_id)
            if unwrapped and unwrapped.startswith("snap-"):
                snap_id = unwrapped
                if not region:
                    region = resource.get("resourceRegion")
                break
        if not snap_id and resource_set:
            first = resource_set[0]
            if isinstance(first, dict):
                snap_id = _unwrap_id(first.get("resourceId"))
                if not region:
                    region = first.get("resourceRegion")

    # Fallback: parse responseElements for SnapshotId
    if not snap_id:
        resp_raw = rec.get("responseElements", "")
        if resp_raw:
            try:
                resp = json.loads(resp_raw) if isinstance(resp_raw, str) else resp_raw
                if isinstance(resp, dict):
                    snap_id = _unwrap_id(resp.get("SnapshotId", ""))
            except Exception:
                pass

    # Fallback: try requestParameters for DiskId-based lookup (snap ID might be absent)
    if not snap_id:
        print(json.dumps({
            "warning": "snapshot_missing_id",
            "region": region,
            "event": event_name
        }))
        return False

    if not region:
        print(json.dumps({
            "warning": "snapshot_missing_region",
            "snap_id": snap_id,
            "event": event_name
        }))
        return False

    # Query snapshot details — try candidate regions
    from index import _region_from_event_source
    cos_region = os.getenv("COS_REGION", "")
    event_source_region = _region_from_event_source(rec.get("eventSource", ""))
    request_region = rec.get("requestRegion") or rec.get("region")

    seen = set()
    candidates = []
    for r in [region, event_source_region, request_region, cos_region]:
        if r and r not in seen:
            seen.add(r)
            candidates.append(r)

    snap_info = None
    actual_region = region
    for candidate in candidates:
        snap_info = get_snapshot_info(snap_id, candidate)
        if snap_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "snapshot_found_in_alternate_region",
                    "snap_id": snap_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break
        if candidate != candidates[-1]:
            print(json.dumps({
                "info": "snapshot_region_retry",
                "snap_id": snap_id,
                "tried": candidate,
                "trying": candidates[candidates.index(candidate) + 1],
                "reason": "snapshot_not_found_in_region"
            }))

    region = actual_region

    disk_id = ""
    disk_usage = ""
    if snap_info:
        disk_id = snap_info.get("DiskId", "") or ""
        disk_usage = snap_info.get("DiskUsage", "") or ""
        print(json.dumps({
            "info": "snapshot_details",
            "snap_id": snap_id,
            "state": snap_info.get("SnapshotState", ""),
            "disk_id": disk_id,
            "disk_usage": disk_usage,
            "disk_size_gb": snap_info.get("DiskSize", 0)
        }))
    else:
        # Try to get DiskId from requestParameters
        req_raw = rec.get("requestParameters", {})
        if isinstance(req_raw, str):
            try:
                req_raw = json.loads(req_raw)
            except Exception:
                req_raw = {}
        if isinstance(req_raw, dict):
            disk_id = req_raw.get("DiskId", "") or ""
        print(json.dumps({
            "warning": "snapshot_info_unavailable",
            "snap_id": snap_id,
            "note": "tagging with defaults in detected region"
        }))

    # Build QCS for snapshot
    # Snapshot uses 'cvm' service namespace in CAM/Tag:
    # qcs::cvm:{region}:uin/{uin}:snapshot/{snap_id}
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::cvm:{region}:uin/{account_uin}:snapshot/{snap_id}"

    owner = get_owner(rec)
    tags = build_snapshot_tags(
        owner=owner,
        disk_id=disk_id,
        disk_usage=disk_usage
    )

    print(json.dumps({
        "info": "snapshot_tagging",
        "snap_id": snap_id,
        "region": region,
        "qcs": qcs
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "snapshot_tagged",
            "snap_id": snap_id,
            "qcs": qcs,
            "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({
            "error": "snapshot_tagging_failed",
            "snap_id": snap_id,
            "qcs": qcs,
            "message": str(e)
        }))
        return False
