"""
HAVIP (High Availability Virtual IP) Tagging Service

Handles tagging for:
- HAVIP instances (CreateHaVip)

HAVIPs are always created standalone and belong to a subnet in a VPC.
They fire a CloudAudit event under ResourceType "vpc".

CloudAudit quirks:
- resourceId may arrive as a stringified Python list: "['havip-xxx']"
- eventSource / eventRegion may point to a different region than where the
  HAVIP actually lives
- Region discovery: tries detected region first, then COS_REGION fallback

QCS format: qcs::vpc:{region}:uin/{uin}:havip/{havip_id}
  (HAVIP uses 'vpc' service namespace in CAM/Tag)
"""

import json
import os
import datetime
from typing import List, Dict, Any, Optional


def build_havip_tags(owner: str, subnet_id: str = "", vpc_id: str = "") -> List[Dict[str, str]]:
    """
    Build tags for HAVIP resources.

    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCreated → TaggerOwner → TaggerProject →
    TaggerSubnet → TaggerTTL → TaggerVpc

    Args:
        owner: Owner email/username
        subnet_id: Subnet ID the HAVIP belongs to
        vpc_id: VPC ID the HAVIP belongs to

    Returns:
        List of tags to apply to HAVIP
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",     "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",   "TagValue": today},
        {"TagKey": "TaggerSubnet",    "TagValue": subnet_id or "unknown"},
        {"TagKey": "TaggerVpc",       "TagValue": vpc_id or "unknown"},
        {"TagKey": "TaggerCanDelete", "TagValue": "YES"},
        {"TagKey": "TaggerTTL",       "TagValue": "3"},
        {"TagKey": "TaggerProject",   "TagValue": "n/a"},
    ]


def get_havip_info(havip_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query HAVIP details using DescribeHaVips API.

    Returns:
        Dict with keys: HaVipId, HaVipName, VpcId, SubnetId, Vip, State,
                        AddressIp
        None if HAVIP not found or error
    """
    from index import make_tc_client
    from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models

    client = make_tc_client("vpc", vpc_client.VpcClient, region)
    if not client:
        return None

    try:
        req = vpc_models.DescribeHaVipsRequest()
        req.HaVipIds = [havip_id]
        resp = client.DescribeHaVips(req)

        havips = getattr(resp, "HaVipSet", [])
        if not havips:
            return None

        havip = havips[0]
        return {
            "HaVipId":    getattr(havip, "HaVipId", ""),
            "HaVipName":  getattr(havip, "HaVipName", ""),
            "VpcId":      getattr(havip, "VpcId", ""),
            "SubnetId":   getattr(havip, "SubnetId", ""),
            "Vip":        getattr(havip, "Vip", ""),
            "State":      getattr(havip, "State", ""),
            "AddressIp":  getattr(havip, "AddressIp", ""),
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_havip_info_failed",
            "havip_id": havip_id,
            "region": region,
            "message": str(e)
        }))
        return None


def handle_havip_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle HAVIP tagging for CreateHaVip events.

    Extraction strategy:
    1. Try resourceSet for HAVIP ID and region
    2. Fallback to responseElements.HaVip.HaVipId
    3. Query HAVIP details for subnet/VPC info
    4. Build tags and apply via Tag API

    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs

    event_name = rec.get("eventName", "")
    if event_name != "CreateHaVip":
        return False

    havip_id = None
    region = extract_region(rec)

    def _unwrap_id(val):
        """Extract a plain string ID from a value that may be a list or string."""
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.startswith("havip-"):
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
            if unwrapped and unwrapped.startswith("havip-"):
                havip_id = unwrapped
                if not region:
                    region = resource.get("resourceRegion")
                break
        if not havip_id and resource_set:
            first = resource_set[0]
            if isinstance(first, dict):
                havip_id = _unwrap_id(first.get("resourceId"))
                if not region:
                    region = first.get("resourceRegion")

    # Fallback: parse responseElements for HaVip
    if not havip_id:
        resp_str = rec.get("responseElements", "")
        if resp_str:
            try:
                resp = json.loads(resp_str) if isinstance(resp_str, str) else resp_str
                havip_obj = resp.get("HaVip", {})
                if isinstance(havip_obj, dict):
                    havip_id = _unwrap_id(havip_obj.get("HaVipId", ""))
            except Exception:
                pass

    if not havip_id or not region:
        print(json.dumps({
            "warning": "havip_missing_id_or_region",
            "havip_id": havip_id,
            "region": region,
            "event": event_name
        }))
        return False

    # Query HAVIP details — try all candidate regions from CloudAudit record
    # CloudAudit quirk: resourceRegion / eventRegion may differ from actual region
    from index import _region_from_event_source
    cos_region = os.getenv("COS_REGION", "")
    event_source_region = _region_from_event_source(rec.get("eventSource", ""))
    request_region = rec.get("requestRegion") or rec.get("region")

    # Build ordered candidate list (deduplicated, skip None/empty)
    seen = set()
    candidates = []
    for r in [region, event_source_region, request_region, cos_region]:
        if r and r not in seen:
            seen.add(r)
            candidates.append(r)

    havip_info = None
    actual_region = region
    for candidate in candidates:
        havip_info = get_havip_info(havip_id, candidate)
        if havip_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "havip_found_in_alternate_region",
                    "havip_id": havip_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break
        if candidate != candidates[-1]:
            print(json.dumps({
                "info": "havip_region_retry",
                "havip_id": havip_id,
                "tried": candidate,
                "trying": candidates[candidates.index(candidate) + 1],
                "reason": "havip_not_found_in_region"
            }))

    region = actual_region

    subnet_id = ""
    vpc_id = ""
    if havip_info:
        subnet_id = havip_info.get("SubnetId", "") or ""
        vpc_id = havip_info.get("VpcId", "") or ""
        print(json.dumps({
            "info": "havip_details",
            "havip_id": havip_id,
            "state": havip_info.get("State", ""),
            "vip": havip_info.get("Vip", ""),
            "vpc_id": vpc_id,
            "subnet_id": subnet_id
        }))
    else:
        print(json.dumps({
            "warning": "havip_info_unavailable",
            "havip_id": havip_id,
            "note": "tagging with defaults in detected region"
        }))

    # Build QCS for HAVIP
    # HAVIP belongs to the VPC service namespace in CAM/Tag:
    # qcs::vpc:{region}:uin/{uin}:havip/{havip_id}
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::vpc:{region}:uin/{account_uin}:havip/{havip_id}"

    owner = get_owner(rec)
    tags = build_havip_tags(
        owner=owner,
        subnet_id=subnet_id,
        vpc_id=vpc_id
    )

    print(json.dumps({
        "info": "havip_tagging",
        "havip_id": havip_id,
        "region": region,
        "qcs": qcs
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "havip_tagged",
            "havip_id": havip_id,
            "qcs": qcs,
            "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({
            "error": "havip_tagging_failed",
            "havip_id": havip_id,
            "qcs": qcs,
            "message": str(e)
        }))
        return False
