"""
Lighthouse (Tencent Cloud Lighthouse) Tagging Service

Handles tagging for:
- Lighthouse instances (CreateInstances)

Lighthouse is a lightweight compute service. Its CloudAudit events fire under
ResourceType "lighthouse". A dedicated tagger-lighthouse-track monitors
CreateInstances events.

CloudAudit quirks:
- responseElements contains InstanceIdSet (array of lhins-xxx strings)
- resourceSet may include the instance with resourceTypeClass containing
  "Instance"
- eventSource: lighthouse.{region}.api.tencentyun.com

QCS format: qcs::lighthouse:{region}:uin/{uin}:instance/{lhins_id}
  (Lighthouse uses its own 'lighthouse' service namespace in CAM/Tag)
"""

import json
import os
import datetime
from typing import List, Dict, Any, Optional


def build_lighthouse_tags(owner: str, instance_name: str = "") -> List[Dict[str, str]]:
    """
    Build tags for Lighthouse instances.

    Tags (displayed alphabetically in console):
    TaggerAutoOff → TaggerAutoStart → TaggerCanDelete → TaggerCreated →
    TaggerInstanceName → TaggerOwner → TaggerProject → TaggerTTL

    Same base tags as CVM (AutoOff, AutoStart) since Lighthouse instances
    support start/stop operations.

    Args:
        owner: Owner email/username
        instance_name: Lighthouse instance display name

    Returns:
        List of tags to apply to Lighthouse instance
    """
    today = datetime.date.today().isoformat()
    tags = [
        {"TagKey": "TaggerOwner",        "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",      "TagValue": today},
        {"TagKey": "TaggerAutoOff",      "TagValue": "YES"},
        {"TagKey": "TaggerAutoStart",    "TagValue": "NO"},
        {"TagKey": "TaggerCanDelete",    "TagValue": "YES"},
        {"TagKey": "TaggerTTL",          "TagValue": "3"},
        {"TagKey": "TaggerProject",      "TagValue": "n/a"},
    ]
    if instance_name:
        tags.append({"TagKey": "TaggerInstanceName", "TagValue": instance_name})
    return tags


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


def handle_lighthouse_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle Lighthouse tagging for CreateInstances events.

    Extraction strategy:
    1. Try resourceSet for instance ID and region
    2. Fallback to responseElements.InstanceIdSet
    3. Query DescribeInstances for details (name, state)
    4. Build tags and apply via Tag API

    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs, _region_from_event_source

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

    if not instance_id or not region:
        print(json.dumps({
            "warning": "lighthouse_missing_id_or_region",
            "instance_id": instance_id,
            "region": region,
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

    instance_name = ""
    if lh_info:
        instance_name = lh_info.get("InstanceName", "") or ""
        print(json.dumps({
            "info": "lighthouse_details",
            "instance_id": instance_id,
            "instance_name": instance_name,
            "state": lh_info.get("InstanceState", ""),
            "zone": lh_info.get("Zone", ""),
            "bundle_id": lh_info.get("BundleId", ""),
            "blueprint_id": lh_info.get("BlueprintId", ""),
        }))
    else:
        # Fallback: try to get name from requestParameters
        req_raw = rec.get("requestParameters", {})
        if isinstance(req_raw, str):
            try:
                req_raw = json.loads(req_raw)
            except Exception:
                req_raw = {}
        if isinstance(req_raw, dict):
            instance_name = req_raw.get("InstanceName", "") or ""
        print(json.dumps({
            "warning": "lighthouse_info_unavailable",
            "instance_id": instance_id,
            "note": "tagging with defaults from request/response params"
        }))

    # Build QCS — Lighthouse uses its own service namespace:
    # qcs::lighthouse:{region}:uin/{uin}:instance/{lhins_id}
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::lighthouse:{region}:uin/{account_uin}:instance/{instance_id}"
    owner = get_owner(rec)
    tags = build_lighthouse_tags(owner=owner, instance_name=instance_name)

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
