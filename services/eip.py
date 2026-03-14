"""
EIP (Elastic IP) Tagging Service

Handles tagging for:
- EIP addresses (AllocateAddresses)

EIPs can be created standalone or by services (CVM). When allocated via
AllocateAddresses, they fire a CloudAudit event under ResourceType "vpc".

CloudAudit quirks:
- resourceId arrives as a stringified Python list: "['eip-xxx']"
- eventSource / eventRegion may point to a different region than where the
  EIP actually lives (e.g. ap-singapore even for eu-frankfurt resources)
- Region discovery: tries detected region first, then COS_REGION fallback

EIP types: EIP, AnycastEIP, HighQualityEIP, AntiDDoSEIP
EIP statuses: CREATING, BINDING, BIND, UNBINDING, UNBIND, OFFLINING, BIND_ENI

QCS format: qcs::cvm:{region}:uin/{uin}:eip/{eip_id}
  (EIP uses 'cvm' service namespace in CAM/Tag, NOT 'eip' or 'vpc')
"""

import json
import os
import datetime
from typing import List, Dict, Any, Optional


def build_eip_tags(owner: str, eip_type: str = "EIP", linked_resource: str = "") -> List[Dict[str, str]]:
    """
    Build tags for EIP resources.
    
    EIPs cannot be stopped/started, so no AutoOff/AutoStart tags.
    
    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCreated → TaggerLinkedResource → TaggerOwner → 
    TaggerProject → TaggerTTL → TaggerType
    
    Args:
        owner: Owner email/username
        eip_type: EIP type (EIP, AnycastEIP, HighQualityEIP, AntiDDoSEIP)
        linked_resource: Bound instance ID or empty string
    
    Returns:
        List of tags to apply to EIP
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",          "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",        "TagValue": today},
        {"TagKey": "TaggerType",           "TagValue": eip_type or "EIP"},
        {"TagKey": "TaggerLinkedResource", "TagValue": linked_resource or "NONE"},
        {"TagKey": "TaggerCanDelete",      "TagValue": "YES"},
        {"TagKey": "TaggerTTL",            "TagValue": "7"},
        {"TagKey": "TaggerProject",        "TagValue": "n/a"},
    ]


def get_eip_info(address_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query EIP details using DescribeAddresses API.
    
    Returns:
        Dict with keys: AddressId, AddressIp, AddressStatus, AddressType,
                        InstanceId, CreatedTime
        None if EIP not found or error
    """
    from index import make_tc_client
    from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models

    client = make_tc_client("vpc", vpc_client.VpcClient, region)
    if not client:
        return None

    try:
        req = vpc_models.DescribeAddressesRequest()
        req.AddressIds = [address_id]
        resp = client.DescribeAddresses(req)

        addresses = getattr(resp, "AddressSet", [])
        if not addresses:
            return None

        addr = addresses[0]
        return {
            "AddressId":     getattr(addr, "AddressId", ""),
            "AddressIp":     getattr(addr, "AddressIp", ""),
            "AddressStatus": getattr(addr, "AddressStatus", ""),
            "AddressType":   getattr(addr, "AddressType", "EIP"),
            "InstanceId":    getattr(addr, "InstanceId", ""),
            "CreatedTime":   getattr(addr, "CreatedTime", ""),
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_eip_info_failed",
            "address_id": address_id,
            "region": region,
            "message": str(e)
        }))
        return None


def handle_eip_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle EIP tagging for AllocateAddresses events.
    
    Extraction strategy:
    1. Try resourceSet for EIP ID and region
    2. Fallback to responseElements.AddressSet
    3. Query EIP details for type and bound status
    4. Build tags and apply via Tag API
    
    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs

    event_name = rec.get("eventName", "")
    if event_name != "AllocateAddresses":
        return False

    eip_id = None
    region = extract_region(rec)

    def _unwrap_id(val):
        """Extract a plain string ID from a value that may be a list or string."""
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.startswith("eip-"):
                    return item
            return val[0] if val and isinstance(val[0], str) else None
        if isinstance(val, str):
            # Handle stringified list: "['eip-xxx']"
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
            if unwrapped and unwrapped.startswith("eip-"):
                eip_id = unwrapped
                if not region:
                    region = resource.get("resourceRegion")
                break
        # If no eip- prefixed ID found, take first resource
        if not eip_id and resource_set:
            first = resource_set[0]
            if isinstance(first, dict):
                eip_id = _unwrap_id(first.get("resourceId"))
                if not region:
                    region = first.get("resourceRegion")

    # Fallback: parse responseElements for AddressSet
    if not eip_id:
        resp_str = rec.get("responseElements", "")
        if resp_str:
            try:
                resp = json.loads(resp_str) if isinstance(resp_str, str) else resp_str
                address_set = resp.get("AddressSet", [])
                if address_set and isinstance(address_set, list):
                    first_addr = address_set[0]
                    if isinstance(first_addr, dict):
                        eip_id = _unwrap_id(first_addr.get("AddressId", ""))
                    elif isinstance(first_addr, str):
                        eip_id = first_addr
            except Exception:
                pass

    if not eip_id or not region:
        print(json.dumps({
            "warning": "eip_missing_id_or_region",
            "eip_id": eip_id,
            "region": region,
            "event": event_name
        }))
        return False

    # Query EIP details — try detected region first, then discover actual region
    # CloudAudit often reports a different region than where the resource lives
    cos_region = os.getenv("COS_REGION", "")
    eip_info = get_eip_info(eip_id, region)
    actual_region = region

    if not eip_info and cos_region and cos_region != region:
        print(json.dumps({
            "info": "eip_region_retry",
            "eip_id": eip_id,
            "tried": region,
            "trying": cos_region,
            "reason": "eip_not_found_in_event_region"
        }))
        eip_info = get_eip_info(eip_id, cos_region)
        if eip_info:
            actual_region = cos_region
            print(json.dumps({
                "info": "eip_found_in_cos_region",
                "eip_id": eip_id,
                "corrected_region": cos_region
            }))

    region = actual_region

    eip_type = "EIP"
    linked_resource = ""

    if eip_info:
        eip_type = eip_info.get("AddressType", "EIP") or "EIP"
        linked_resource = eip_info.get("InstanceId", "") or ""
        print(json.dumps({
            "info": "eip_details",
            "eip_id": eip_id,
            "eip_ip": eip_info.get("AddressIp", ""),
            "eip_type": eip_type,
            "eip_status": eip_info.get("AddressStatus", ""),
            "linked_resource": linked_resource or "NONE"
        }))
    else:
        print(json.dumps({
            "warning": "eip_info_unavailable",
            "eip_id": eip_id,
            "note": "tagging with defaults in detected region"
        }))

    # Build QCS for EIP
    # EIP belongs to the CVM service namespace in CAM/Tag:
    # qcs::cvm:{region}:uin/{uin}:eip/{eip_id}
    # (confirmed by CloudAudit "resources" field which uses qcs::cvm:...:eip/*)
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::cvm:{region}:uin/{account_uin}:eip/{eip_id}"

    owner = get_owner(rec)
    tags = build_eip_tags(
        owner=owner,
        eip_type=eip_type,
        linked_resource=linked_resource
    )

    print(json.dumps({
        "info": "eip_tagging",
        "eip_id": eip_id,
        "region": region,
        "qcs": qcs,
        "eip_type": eip_type
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "eip_tagged",
            "eip_id": eip_id,
            "qcs": qcs,
            "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({
            "error": "eip_tagging_failed",
            "eip_id": eip_id,
            "qcs": qcs,
            "message": str(e)
        }))
        return False
