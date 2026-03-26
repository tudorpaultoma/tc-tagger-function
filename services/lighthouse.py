"""
Lighthouse (Tencent Cloud Lighthouse) Tagging Service

Handles tagging for:
- Lighthouse instances (CreateInstances)

Lighthouse is a lightweight compute service. Its CloudAudit events fire under
ResourceType "lighthouse". A dedicated tagger-lighthouse-track monitors
CreateInstances events.

CloudAudit quirks:
- CreateInstances is ASYNC — CloudAudit delivers the event before instance
  provisioning completes, so resourceSet is typically [] and responseElements
  is "{}".  The instance ID (lhins-xxx) is NOT in the event.
- Discovery fallback: when no instance ID is available, we call
  DescribeInstances to list all instances in the event region and tag any
  that are missing Tagger* tags.
- If resourceSet/responseElements do contain IDs (future CA improvement),
  the direct-ID path is still used.
- eventSource: lighthouse.tencentcloudapi.com (no embedded region)

QCS format: qcs::lighthouse:{region}:uin/{uin}:instance/{lhins_id}
  (Lighthouse uses its own 'lighthouse' service namespace in CAM/Tag)
"""

import json
import os
import datetime
from typing import List, Dict, Any, Optional


def build_lighthouse_tags(owner: str) -> List[Dict[str, str]]:
    """
    Build tags for Lighthouse instances.

    Tags (displayed alphabetically in console):
    TaggerAutoOff → TaggerAutoStart → TaggerCanDelete → TaggerCreated →
    TaggerOwner → TaggerProject → TaggerTTL

    Same base tags as CVM (AutoOff, AutoStart) since Lighthouse instances
    support start/stop operations.

    Args:
        owner: Owner email/username

    Returns:
        List of tags to apply to Lighthouse instance
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",        "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",      "TagValue": today},
        {"TagKey": "TaggerAutoOff",      "TagValue": "YES"},
        {"TagKey": "TaggerAutoStart",    "TagValue": "NO"},
        {"TagKey": "TaggerCanDelete",    "TagValue": "YES"},
        {"TagKey": "TaggerTTL",          "TagValue": "3"},
        {"TagKey": "TaggerProject",      "TagValue": "n/a"},
    ]


def build_lighthouse_snapshot_tags(owner: str, instance_id: str = "") -> List[Dict[str, str]]:
    """
    Build tags for Lighthouse snapshots.

    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCreated → TaggerOwner →
    TaggerProject → TaggerSourceInstance → TaggerTTL

    Args:
        owner: Owner email/username
        instance_id: Source Lighthouse instance ID (lhins-xxx)

    Returns:
        List of tags to apply to Lighthouse snapshot
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",          "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",        "TagValue": today},
        {"TagKey": "TaggerSourceInstance", "TagValue": instance_id or "unknown"},
        {"TagKey": "TaggerCanDelete",      "TagValue": "YES"},
        {"TagKey": "TaggerTTL",            "TagValue": "3"},
        {"TagKey": "TaggerProject",        "TagValue": "n/a"},
    ]


def get_lighthouse_info(instance_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query Lighthouse instance details using DescribeInstances API.

    Returns:
        Dict with keys: InstanceId, InstanceName, InstanceState, Zone,
                        BundleId, BlueprintId
        None if instance not found or error
    """
    from index import make_tc_client
    from tencentcloud.lighthouse.v20200324 import lighthouse_client, models as lh_models

    client = make_tc_client("lighthouse", lighthouse_client.LighthouseClient, region)
    if not client:
        return None

    try:
        req = lh_models.DescribeInstancesRequest()
        req.InstanceIds = [instance_id]
        resp = client.DescribeInstances(req)

        instances = getattr(resp, "InstanceSet", [])
        if not instances:
            return None

        inst = instances[0]
        return {
            "InstanceId":    getattr(inst, "InstanceId", ""),
            "InstanceName":  getattr(inst, "InstanceName", ""),
            "InstanceState": getattr(inst, "InstanceState", ""),
            "Zone":          getattr(inst, "Zone", ""),
            "BundleId":      getattr(inst, "BundleId", ""),
            "BlueprintId":   getattr(inst, "BlueprintId", ""),
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_lighthouse_info_failed",
            "instance_id": instance_id,
            "region": region,
            "message": str(e)
        }))
        return None


def discover_recent_lighthouse_instances(region: str) -> List[Dict[str, Any]]:
    """
    Discover all Lighthouse instances in a region via DescribeInstances.

    Lighthouse CreateInstances is async — CloudAudit often delivers the event
    with empty resourceSet and responseElements (no instance ID).  This
    discovery function lists all instances in the region so the caller can
    find recently-created ones that still lack Tagger tags.

    Returns:
        List of instance dicts (same shape as get_lighthouse_info), empty on error.
    """
    from index import make_tc_client
    from tencentcloud.lighthouse.v20200324 import lighthouse_client, models as lh_models

    client = make_tc_client("lighthouse", lighthouse_client.LighthouseClient, region)
    if not client:
        return []

    try:
        req = lh_models.DescribeInstancesRequest()
        req.Limit = 100
        resp = client.DescribeInstances(req)

        results = []
        for inst in getattr(resp, "InstanceSet", []) or []:
            results.append({
                "InstanceId":    getattr(inst, "InstanceId", ""),
                "InstanceName":  getattr(inst, "InstanceName", ""),
                "InstanceState": getattr(inst, "InstanceState", ""),
                "Zone":          getattr(inst, "Zone", ""),
                "BundleId":      getattr(inst, "BundleId", ""),
                "BlueprintId":   getattr(inst, "BlueprintId", ""),
                "Tags":          _extract_instance_tags(inst),
                "CreatedTime":   getattr(inst, "CreatedTime", ""),
            })
        return results
    except Exception as e:
        print(json.dumps({
            "error": "discover_lighthouse_instances_failed",
            "region": region,
            "message": str(e)
        }))
        return []


def _extract_instance_tags(inst) -> List[Dict[str, str]]:
    """Extract tag list from a Lighthouse instance object."""
    raw_tags = getattr(inst, "Tags", []) or []
    tags = []
    for t in raw_tags:
        key = getattr(t, "Key", "") or getattr(t, "TagKey", "")
        val = getattr(t, "Value", "") or getattr(t, "TagValue", "")
        if key:
            tags.append({"TagKey": key, "TagValue": val})
    return tags


def _instance_has_tagger_tags(tags: List[Dict[str, str]]) -> bool:
    """Check if an instance already has any Tagger* tags."""
    return any(t.get("TagKey", "").startswith("Tagger") for t in tags)


def _safe_dict(val):
    """Return val as a dict — parsing from JSON string if needed."""
    if isinstance(val, dict):
        return val
    if isinstance(val, str) and val:
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _unwrap_id(val):
    """Extract a plain string ID from a value that may be a list or string."""
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str) and item.startswith("lhins-"):
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


def _tag_single_lighthouse(instance_id: str, region: str, account_uin: str,
                            owner: str) -> bool:
    """
    Build QCS, tags, and apply them for a single Lighthouse instance.

    Returns True on success.
    """
    from index import tag_resource_qcs

    qcs = f"qcs::lighthouse:{region}:uin/{account_uin}:instance/{instance_id}"
    tags = build_lighthouse_tags(owner=owner)

    print(json.dumps({
        "info": "lighthouse_tagging",
        "instance_id": instance_id,
        "region": region,
        "qcs": qcs
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "lighthouse_tagged",
            "instance_id": instance_id,
            "qcs": qcs,
            "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({
            "error": "lighthouse_tagging_failed",
            "instance_id": instance_id,
            "qcs": qcs,
            "message": str(e)
        }))
        return False


def handle_lighthouse_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle Lighthouse tagging for CreateInstances events.

    Extraction strategy:
    1. Try resourceSet for instance ID and region
    2. Fallback to responseElements.InstanceIdSet
    3. If neither has an ID (Lighthouse CreateInstances is async and CloudAudit
       often delivers empty resourceSet/responseElements), fall back to
       discovery: list all instances in the event region and tag any that are
       missing Tagger tags.
    4. Query DescribeInstances for details (name, state) when we have an ID
    5. Build tags and apply via Tag API

    Returns:
        True if at least one instance was tagged, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, _region_from_event_source

    event_name = rec.get("eventName", "")
    if event_name != "CreateInstances":
        return False

    # Must be lighthouse service — CreateInstances is also used by CVM
    event_source = rec.get("eventSource", "")
    resource_type = rec.get("resourceType", "")
    if resource_type and resource_type.lower() != "lighthouse":
        return False
    if not resource_type and event_source and "lighthouse" not in event_source.lower():
        return False

    instance_id = None
    region = extract_region(rec)

    # Try resourceSet first
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list):
        for resource in resource_set:
            if not isinstance(resource, dict):
                continue
            raw_id = resource.get("resourceId", "")
            unwrapped = _unwrap_id(raw_id)
            if unwrapped and unwrapped.startswith("lhins-"):
                instance_id = unwrapped
                if not region:
                    region = resource.get("resourceRegion")
                break
        if not instance_id and resource_set:
            first = resource_set[0]
            if isinstance(first, dict):
                instance_id = _unwrap_id(first.get("resourceId"))
                if not region:
                    region = first.get("resourceRegion")

    # Fallback: parse responseElements for InstanceIdSet
    if not instance_id:
        resp_elems = _safe_dict(rec.get("responseElements"))
        id_set = resp_elems.get("InstanceIdSet", [])
        if isinstance(id_set, list) and id_set:
            instance_id = _unwrap_id(id_set[0])
        elif isinstance(id_set, str):
            instance_id = _unwrap_id(id_set)

    # -------------------------------------------------------------------
    # Discovery fallback: Lighthouse CreateInstances is async — CA event
    # typically has empty resourceSet and responseElements.  When we have
    # no instance ID we list all instances in the region and tag any that
    # are still missing Tagger tags.
    # -------------------------------------------------------------------
    if not instance_id:
        if not region:
            # Last-resort region: eventRegion is always present
            region = rec.get("eventRegion")
        if not region:
            print(json.dumps({
                "warning": "lighthouse_missing_id_and_region",
                "event": event_name
            }))
            return False

        print(json.dumps({
            "info": "lighthouse_discovery_fallback",
            "region": region,
            "reason": "empty resourceSet and responseElements"
        }))

        account_uin = extract_account_uin(rec)
        owner = get_owner(rec)
        instances = discover_recent_lighthouse_instances(region)

        if not instances:
            print(json.dumps({
                "warning": "lighthouse_discovery_no_instances",
                "region": region
            }))
            return False

        any_tagged = False
        for inst in instances:
            if _instance_has_tagger_tags(inst.get("Tags", [])):
                continue
            iid = inst.get("InstanceId", "")
            if not iid:
                continue
            print(json.dumps({
                "info": "lighthouse_discovery_tagging",
                "instance_id": iid,
                "instance_name": inst.get("InstanceName", ""),
                "state": inst.get("InstanceState", ""),
                "created_time": inst.get("CreatedTime", ""),
            }))
            if _tag_single_lighthouse(iid, region, account_uin, owner):
                any_tagged = True

        return any_tagged

    # -------------------------------------------------------------------
    # Normal path: we have an instance ID
    # -------------------------------------------------------------------
    if not region:
        print(json.dumps({
            "warning": "lighthouse_missing_region",
            "instance_id": instance_id,
            "event": event_name
        }))
        return False

    # Query instance details — try candidate regions
    cos_region = os.getenv("COS_REGION", "")
    event_source_region = _region_from_event_source(rec.get("eventSource", ""))
    request_region = rec.get("requestRegion") or rec.get("region")

    seen = set()
    candidates = []
    for r in [region, event_source_region, request_region, cos_region]:
        if r and r not in seen:
            seen.add(r)
            candidates.append(r)

    lh_info = None
    actual_region = region
    for candidate in candidates:
        lh_info = get_lighthouse_info(instance_id, candidate)
        if lh_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "lighthouse_found_in_alternate_region",
                    "instance_id": instance_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break
        if candidate != candidates[-1]:
            print(json.dumps({
                "info": "lighthouse_region_retry",
                "instance_id": instance_id,
                "tried": candidate,
                "reason": "instance_not_found_in_region"
            }))

    region = actual_region

    if lh_info:
        print(json.dumps({
            "info": "lighthouse_details",
            "instance_id": instance_id,
            "instance_name": lh_info.get("InstanceName", ""),
            "state": lh_info.get("InstanceState", ""),
            "zone": lh_info.get("Zone", ""),
            "bundle_id": lh_info.get("BundleId", ""),
            "blueprint_id": lh_info.get("BlueprintId", ""),
        }))
    else:
        print(json.dumps({
            "warning": "lighthouse_info_unavailable",
            "instance_id": instance_id,
            "note": "tagging with defaults"
        }))

    account_uin = extract_account_uin(rec)
    owner = get_owner(rec)
    return _tag_single_lighthouse(instance_id, region, account_uin, owner)


# ---------------------------------------------------------------------------
# Lighthouse Snapshot support
# ---------------------------------------------------------------------------

def get_lighthouse_snapshot_info(snapshot_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query Lighthouse snapshot details using lighthouse:DescribeSnapshots API.

    Returns:
        Dict with keys: SnapshotId, SnapshotName, DiskId, DiskUsage,
                        DiskSize, SnapshotState, CreatedTime
        None if snapshot not found or error
    """
    from index import make_tc_client
    from tencentcloud.lighthouse.v20200324 import lighthouse_client, models as lh_models

    client = make_tc_client("lighthouse", lighthouse_client.LighthouseClient, region)
    if not client:
        return None

    try:
        req = lh_models.DescribeSnapshotsRequest()
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
            "DiskSize":      getattr(snap, "DiskSize", 0),
            "SnapshotState": getattr(snap, "SnapshotState", ""),
            "CreatedTime":   getattr(snap, "CreatedTime", ""),
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_lighthouse_snapshot_info_failed",
            "snapshot_id": snapshot_id,
            "region": region,
            "message": str(e)
        }))
        return None


def handle_lighthouse_snapshot_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle Lighthouse snapshot tagging for CreateInstanceSnapshot events.

    CloudAudit event:
    - eventName: CreateInstanceSnapshot
    - resourceType: lighthouse
    - responseElements contains SnapshotId (lhsnap-xxx)
    - requestParameters contains InstanceId (lhins-xxx)

    QCS format: qcs::lighthouse:{region}:uin/{uin}:snapshot/{lhsnap_id}

    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs, _region_from_event_source

    event_name = rec.get("eventName", "")
    if event_name != "CreateInstanceSnapshot":
        return False

    snap_id = None
    instance_id = None
    region = extract_region(rec)

    # Try resourceSet first
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list):
        for resource in resource_set:
            if not isinstance(resource, dict):
                continue
            raw_id = resource.get("resourceId", "")
            unwrapped = _unwrap_id(raw_id)
            if unwrapped and isinstance(unwrapped, str) and unwrapped.startswith("lhsnap-"):
                snap_id = unwrapped
                if not region:
                    region = resource.get("resourceRegion")
                break

    # Fallback: parse responseElements for SnapshotId
    if not snap_id:
        resp_elems = _safe_dict(rec.get("responseElements"))
        raw_snap = resp_elems.get("SnapshotId", "")
        if isinstance(raw_snap, str) and raw_snap.startswith("lhsnap-"):
            snap_id = raw_snap

    # Extract InstanceId from requestParameters
    req_raw = rec.get("requestParameters", {})
    if isinstance(req_raw, str):
        try:
            req_raw = json.loads(req_raw)
        except Exception:
            req_raw = {}
    if isinstance(req_raw, dict):
        instance_id = req_raw.get("InstanceId", "") or ""

    if not snap_id:
        print(json.dumps({
            "warning": "lighthouse_snapshot_missing_id",
            "region": region,
            "event": event_name
        }))
        return False

    if not region:
        region = rec.get("eventRegion")
    if not region:
        print(json.dumps({
            "warning": "lighthouse_snapshot_missing_region",
            "snap_id": snap_id,
            "event": event_name
        }))
        return False

    # Query snapshot details — try candidate regions
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
        snap_info = get_lighthouse_snapshot_info(snap_id, candidate)
        if snap_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "lighthouse_snapshot_found_in_alternate_region",
                    "snap_id": snap_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break
        if candidate != candidates[-1]:
            print(json.dumps({
                "info": "lighthouse_snapshot_region_retry",
                "snap_id": snap_id,
                "tried": candidate,
                "reason": "snapshot_not_found_in_region"
            }))

    region = actual_region

    if snap_info:
        print(json.dumps({
            "info": "lighthouse_snapshot_details",
            "snap_id": snap_id,
            "state": snap_info.get("SnapshotState", ""),
            "disk_size_gb": snap_info.get("DiskSize", 0),
        }))
    else:
        print(json.dumps({
            "warning": "lighthouse_snapshot_info_unavailable",
            "snap_id": snap_id,
            "note": "tagging with defaults"
        }))

    # Build QCS — Lighthouse snapshots use lighthouse service namespace:
    # qcs::lighthouse:{region}:uin/{uin}:snapshot/{lhsnap_id}
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::lighthouse:{region}:uin/{account_uin}:snapshot/{snap_id}"

    owner = get_owner(rec)
    tags = build_lighthouse_snapshot_tags(
        owner=owner,
        instance_id=instance_id or "",
    )

    print(json.dumps({
        "info": "lighthouse_snapshot_tagging",
        "snap_id": snap_id,
        "region": region,
        "qcs": qcs
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "lighthouse_snapshot_tagged",
            "snap_id": snap_id,
            "qcs": qcs,
            "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({
            "error": "lighthouse_snapshot_tagging_failed",
            "snap_id": snap_id,
            "qcs": qcs,
            "message": str(e)
        }))
        return False
