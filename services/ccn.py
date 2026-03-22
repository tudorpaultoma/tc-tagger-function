"""
CCN (Cloud Connect Network) Tagging Service

Handles tagging for:
- CCN instances (CreateCcn)

CCN provides full-mesh interconnection between VPCs across regions and
between VPCs and on-premises data centers. CCN events fire under
ResourceType "vpc" in CloudAudit.

CloudAudit quirks:
- CreatePrivateNatGateway also references CCN in its `resources` field
  (qcs::vpc:{region}:uin/{uin}:ccn/{ccn_id}) but is handled by nat.py
- CreateCcn typically returns CcnId in responseElements
- resourceSet may be empty; ID comes from responseElements

QCS format: qcs::vpc:{region}:uin/{uin}:ccn/{ccn_id}
  (CCN uses 'vpc' service namespace in CAM/Tag)
"""

import json
import os
import datetime
from typing import List, Dict, Any, Optional


def build_ccn_tags(owner: str, ccn_name: str = "", instance_count: int = 0,
                   qos_level: str = "") -> List[Dict[str, str]]:
    """
    Build tags for CCN resources.

    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCcnName → TaggerCreated →
    TaggerOwner → TaggerProject → TaggerTTL

    Args:
        owner: Owner email/username
        ccn_name: CCN instance name
        instance_count: Number of associated network instances
        qos_level: QoS level (PT, AU, AG)

    Returns:
        List of tags to apply to CCN instance
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",      "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",    "TagValue": today},
        {"TagKey": "TaggerCcnName",    "TagValue": ccn_name or "unknown"},
        {"TagKey": "TaggerCanDelete",  "TagValue": "YES"},
        {"TagKey": "TaggerTTL",        "TagValue": "3"},
        {"TagKey": "TaggerProject",    "TagValue": "n/a"},
    ]


def get_ccn_info(ccn_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query CCN details using DescribeCcns API.

    The DescribeCcns API is region-independent (CCN is global), but the
    SDK still requires a region parameter for the API endpoint.

    Returns:
        Dict with keys: CcnId, CcnName, CcnDescription, State,
                        InstanceCount, QosLevel, BandwidthLimitType
        None if CCN not found or error
    """
    from index import make_tc_client
    from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models

    client = make_tc_client("vpc", vpc_client.VpcClient, region)
    if not client:
        return None

    try:
        req = vpc_models.DescribeCcnsRequest()
        req.CcnIds = [ccn_id]
        resp = client.DescribeCcns(req)

        ccns = getattr(resp, "CcnSet", [])
        if not ccns:
            return None

        ccn = ccns[0]
        return {
            "CcnId":              getattr(ccn, "CcnId", ""),
            "CcnName":            getattr(ccn, "CcnName", ""),
            "CcnDescription":     getattr(ccn, "CcnDescription", ""),
            "State":              getattr(ccn, "State", ""),
            "InstanceCount":      getattr(ccn, "InstanceCount", 0),
            "QosLevel":           getattr(ccn, "QosLevel", ""),
            "BandwidthLimitType": getattr(ccn, "BandwidthLimitType", ""),
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_ccn_info_failed",
            "ccn_id": ccn_id,
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


def handle_ccn_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle CCN tagging for CreateCcn events.

    Extraction strategy:
    1. Try resourceSet for CCN ID and region
    2. Fallback to responseElements.CcnId
    3. Query DescribeCcns for details
    4. Build tags and apply via Tag API

    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs, _region_from_event_source

    event_name = rec.get("eventName", "")
    if event_name != "CreateCcn":
        return False

    resp_elems = _safe_dict(rec.get("responseElements"))

    # Extract CCN ID from resourceSet
    ccn_id = None
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list):
        for resource in resource_set:
            if not isinstance(resource, dict):
                continue
            raw_id = resource.get("resourceId", "")
            if isinstance(raw_id, str) and raw_id.startswith("ccn-"):
                ccn_id = raw_id
                break

    # Fallback: parse responseElements
    if not ccn_id:
        ccn_obj = resp_elems.get("Ccn") or {}
        if isinstance(ccn_obj, dict):
            ccn_id = ccn_obj.get("CcnId", "")
        if not ccn_id:
            ccn_id = resp_elems.get("CcnId", "")

    # Region — CCN is global but we still need a region for the Tag API call
    region = extract_region(rec)
    if not region:
        # CCN API is region-independent; use COS_REGION as default
        region = os.getenv("COS_REGION", "ap-singapore")

    if not ccn_id:
        print(json.dumps({
            "warning": "ccn_missing_id",
            "region": region, "event": event_name
        }))
        return False

    # Query CCN details — try candidate regions
    cos_region = os.getenv("COS_REGION", "")
    event_source_region = _region_from_event_source(rec.get("eventSource", ""))

    seen = set()
    candidates = []
    for r in [region, cos_region, event_source_region]:
        if r and r not in seen:
            seen.add(r)
            candidates.append(r)

    ccn_info = None
    actual_region = region
    for candidate in candidates:
        ccn_info = get_ccn_info(ccn_id, candidate)
        if ccn_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "ccn_found_in_alternate_region",
                    "ccn_id": ccn_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break

    region = actual_region

    ccn_name = ""
    instance_count = 0
    qos_level = ""
    if ccn_info:
        ccn_name = ccn_info.get("CcnName", "") or ""
        instance_count = ccn_info.get("InstanceCount", 0) or 0
        qos_level = ccn_info.get("QosLevel", "") or ""
        print(json.dumps({
            "info": "ccn_details",
            "ccn_id": ccn_id,
            "ccn_name": ccn_name,
            "state": ccn_info.get("State", ""),
            "instance_count": instance_count,
            "qos_level": qos_level,
            "bandwidth_limit_type": ccn_info.get("BandwidthLimitType", ""),
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
            ccn_name = req_raw.get("CcnName", "") or ""
        print(json.dumps({
            "warning": "ccn_info_unavailable",
            "ccn_id": ccn_id,
            "note": "tagging with defaults from request/response params"
        }))

    # Build QCS — CCN uses vpc service namespace:
    # qcs::vpc:{region}:uin/{uin}:ccn/{ccn_id}
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::vpc:{region}:uin/{account_uin}:ccn/{ccn_id}"
    owner = get_owner(rec)
    tags = build_ccn_tags(
        owner=owner,
        ccn_name=ccn_name,
        instance_count=instance_count,
        qos_level=qos_level,
    )

    print(json.dumps({
        "info": "ccn_tagging", "ccn_id": ccn_id, "region": region, "qcs": qcs
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "ccn_tagged",
            "ccn_id": ccn_id, "qcs": qcs, "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({
            "error": "ccn_tagging_failed",
            "ccn_id": ccn_id, "qcs": qcs, "message": str(e)
        }))
        return False
