"""
ENI (Elastic Network Interface) Tagging Service

Handles tagging for:
- ENI network interfaces (CreateNetworkInterface)

ENIs can be created standalone or alongside CVM instances. When created via
CreateNetworkInterface, they fire a CloudAudit event under ResourceType "vpc".

CloudAudit quirks:
- resourceId may arrive as a stringified Python list: "['eni-xxx']"
- eventSource / eventRegion may point to a different region than where the
  ENI actually lives
- Region discovery: tries detected region first, then COS_REGION fallback

QCS format: qcs::vpc:{region}:uin/{uin}:eni/{eni_id}
  (ENI uses 'vpc' service namespace in CAM/Tag)
"""

import json
import os
import datetime
from typing import List, Dict, Any, Optional


def build_eni_tags(owner: str, linked_resource: str = "") -> List[Dict[str, str]]:
    """
    Build tags for ENI resources.

    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCreated → TaggerLinkedResource → TaggerOwner →
    TaggerProject → TaggerTTL

    Args:
        owner: Owner email/username
        linked_resource: Bound CVM instance ID or empty string

    Returns:
        List of tags to apply to ENI
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",          "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",        "TagValue": today},
        {"TagKey": "TaggerLinkedResource", "TagValue": linked_resource or "NONE"},
        {"TagKey": "TaggerCanDelete",      "TagValue": "YES"},
        {"TagKey": "TaggerTTL",            "TagValue": "3"},
        {"TagKey": "TaggerProject",        "TagValue": "n/a"},
    ]


def get_eni_info(eni_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query ENI details using DescribeNetworkInterfaces API.

    Returns:
        Dict with keys: NetworkInterfaceId, NetworkInterfaceName, State,
                        Attachment (InstanceId), VpcId, SubnetId
        None if ENI not found or error
    """
    from index import make_tc_client
    from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models

    client = make_tc_client("vpc", vpc_client.VpcClient, region)
    if not client:
        return None

    try:
        req = vpc_models.DescribeNetworkInterfacesRequest()
        req.NetworkInterfaceIds = [eni_id]
        resp = client.DescribeNetworkInterfaces(req)

        enis = getattr(resp, "NetworkInterfaceSet", [])
        if not enis:
            return None

        eni = enis[0]
        # Attachment contains InstanceId if ENI is bound to a CVM
        attachment = getattr(eni, "Attachment", None)
        instance_id = getattr(attachment, "InstanceId", "") if attachment else ""

        return {
            "NetworkInterfaceId":   getattr(eni, "NetworkInterfaceId", ""),
            "NetworkInterfaceName": getattr(eni, "NetworkInterfaceName", ""),
            "State":                getattr(eni, "State", ""),
            "InstanceId":           instance_id,
            "VpcId":                getattr(eni, "VpcId", ""),
            "SubnetId":             getattr(eni, "SubnetId", ""),
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_eni_info_failed",
            "eni_id": eni_id,
            "region": region,
            "message": str(e)
        }))
        return None


def handle_eni_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle ENI tagging for CreateNetworkInterface events.

    Extraction strategy:
    1. Try resourceSet for ENI ID and region
    2. Fallback to responseElements.NetworkInterface.NetworkInterfaceId
    3. Query ENI details for attachment info
    4. Build tags and apply via Tag API

    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs

    event_name = rec.get("eventName", "")
    if event_name != "CreateNetworkInterface":
        return False

    eni_id = None
    region = extract_region(rec)

    def _unwrap_id(val):
        """Extract a plain string ID from a value that may be a list or string."""
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.startswith("eni-"):
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
            if unwrapped and unwrapped.startswith("eni-"):
                eni_id = unwrapped
                if not region:
                    region = resource.get("resourceRegion")
                break
        if not eni_id and resource_set:
            first = resource_set[0]
            if isinstance(first, dict):
                eni_id = _unwrap_id(first.get("resourceId"))
                if not region:
                    region = first.get("resourceRegion")

    # Fallback: parse responseElements for NetworkInterface
    if not eni_id:
        resp_str = rec.get("responseElements", "")
        if resp_str:
            try:
                resp = json.loads(resp_str) if isinstance(resp_str, str) else resp_str
                ni = resp.get("NetworkInterface", {})
                if isinstance(ni, dict):
                    eni_id = _unwrap_id(ni.get("NetworkInterfaceId", ""))
            except Exception:
                pass

    if not eni_id or not region:
        print(json.dumps({
            "warning": "eni_missing_id_or_region",
            "eni_id": eni_id,
            "region": region,
            "event": event_name
        }))
        return False

    # Query ENI details — try all candidate regions from CloudAudit record
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

    eni_info = None
    actual_region = region
    for candidate in candidates:
        eni_info = get_eni_info(eni_id, candidate)
        if eni_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "eni_found_in_alternate_region",
                    "eni_id": eni_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break
        if candidate != candidates[-1]:
            print(json.dumps({
                "info": "eni_region_retry",
                "eni_id": eni_id,
                "tried": candidate,
                "trying": candidates[candidates.index(candidate) + 1],
                "reason": "eni_not_found_in_region"
            }))

    region = actual_region

    linked_resource = ""
    if eni_info:
        linked_resource = eni_info.get("InstanceId", "") or ""
        print(json.dumps({
            "info": "eni_details",
            "eni_id": eni_id,
            "state": eni_info.get("State", ""),
            "linked_resource": linked_resource or "NONE",
            "vpc_id": eni_info.get("VpcId", ""),
            "subnet_id": eni_info.get("SubnetId", "")
        }))
    else:
        print(json.dumps({
            "warning": "eni_info_unavailable",
            "eni_id": eni_id,
            "note": "tagging with defaults in detected region"
        }))

    # Build QCS for ENI
    # ENI belongs to the VPC service namespace in CAM/Tag:
    # qcs::vpc:{region}:uin/{uin}:eni/{eni_id}
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::vpc:{region}:uin/{account_uin}:eni/{eni_id}"

    owner = get_owner(rec)
    tags = build_eni_tags(
        owner=owner,
        linked_resource=linked_resource
    )

    print(json.dumps({
        "info": "eni_tagging",
        "eni_id": eni_id,
        "region": region,
        "qcs": qcs
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "eni_tagged",
            "eni_id": eni_id,
            "qcs": qcs,
            "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({
            "error": "eni_tagging_failed",
            "eni_id": eni_id,
            "qcs": qcs,
            "message": str(e)
        }))
        return False
