"""
EIP (Elastic IP) Tagging Service

Handles tagging for:
- EIP addresses (AllocateAddresses)
- Public IP → EIP conversions (TransformAddress)

EIPs can be created standalone or by services (CVM). When allocated via
AllocateAddresses, they fire a CloudAudit event under ResourceType "vpc".
When a regular public IP is converted to EIP via TransformAddress (console
or API), a separate CloudAudit event fires with the CVM InstanceId in
requestParameters and the new AddressId in responseElements.

CloudAudit quirks for EIP events:
- resourceId arrives as a stringified Python list: "['eip-xxx']"
- eventSource / eventRegion may report wrong region (e.g. ap-singapore for
  eu-frankfurt resources) — region probing across all regions is used
- requestParameters / responseElements arrive as JSON strings in COS files
  (not parsed dicts) — must be deserialized before access
- TransformAddress: AddressId is masked as '***', resourceSet is empty,
  and region fields are unreliable — EIP is discovered via DescribeAddresses
  with instance-id filter across all international regions

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
        {"TagKey": "TaggerTTL",            "TagValue": "3"},
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


def get_eip_by_instance_id(instance_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Find EIP bound to a CVM instance using DescribeAddresses with instance-id filter.
    Used for TransformAddress where CloudAudit masks the AddressId as '***'.

    Returns:
        Dict with EIP details, or None if not found
    """
    from index import make_tc_client
    from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models

    client = make_tc_client("vpc", vpc_client.VpcClient, region)
    if not client:
        return None

    try:
        req = vpc_models.DescribeAddressesRequest()
        f = vpc_models.Filter()
        f.Name = "instance-id"
        f.Values = [instance_id]
        req.Filters = [f]
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
            "error": "get_eip_by_instance_failed",
            "instance_id": instance_id,
            "region": region,
            "message": str(e)
        }))
        return None


def handle_eip_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle EIP tagging for AllocateAddresses and TransformAddress events.
    
    AllocateAddresses extraction strategy:
    1. Try resourceSet for EIP ID and region
    2. Fallback to responseElements.AddressSet
    3. Query EIP details for type and bound status
    4. Build tags and apply via Tag API
    
    TransformAddress extraction strategy:
    1. Parse requestParameters (JSON string in COS) for InstanceId
    2. Try responseElements.AddressId (usually masked as '***')
    3. Fallback: probe DescribeAddresses with instance-id filter across all regions
    4. Region resolved from successful probe hit
    
    Note: COS-delivered CloudAudit events store requestParameters and
    responseElements as JSON strings, not dicts. The _safe_dict() helper
    handles transparent deserialization.
    
    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs

    event_name = rec.get("eventName", "")
    if event_name not in ("AllocateAddresses", "TransformAddress"):
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

    # --- Helper: safely parse JSON-string fields ---
    # COS-delivered CloudAudit events store requestParameters / responseElements
    # as JSON *strings*, not dicts.  The CA console shows them parsed, but the
    # actual COS object keeps them stringified.
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

    req_params = _safe_dict(rec.get("requestParameters"))
    resp_elems = _safe_dict(rec.get("responseElements"))

    # --- TransformAddress: AddressId is masked as '***' by CloudAudit ---
    # Strategy: use InstanceId to discover the EIP via DescribeAddresses filter
    if event_name == "TransformAddress":
        source_instance = req_params.get("InstanceId", "")

        # Try extracting AddressId from responseElements (in case it's not masked)
        addr_id = resp_elems.get("AddressId", "")
        if addr_id and isinstance(addr_id, str) and addr_id.startswith("eip-"):
            eip_id = addr_id

        # AddressId masked — discover EIP by querying instance binding
        if not eip_id and source_instance:
            from index import _region_from_event_source
            cos_region = os.getenv("COS_REGION", "")
            es_region = _region_from_event_source(rec.get("eventSource", ""))
            evt_region = rec.get("eventRegion")
            params_region = req_params.get("Region", "")

            # TransformAddress eventSource/eventRegion are unreliable (often ap-singapore).
            # Probe all international regions with best guesses first.
            ALL_INTL_REGIONS = [
                "eu-frankfurt", "ap-singapore", "ap-hongkong", "ap-bangkok",
                "ap-beijing", "ap-chengdu", "ap-chongqing", "ap-guangzhou",
                "ap-jakarta", "ap-mumbai", "ap-nanjing", "ap-seoul",
                "ap-shanghai", "ap-tokyo", "eu-moscow", "na-ashburn",
                "na-siliconvalley", "na-toronto", "sa-saopaulo",
            ]

            # Build candidate regions: known hints first, then remaining regions
            seen_r = set()
            probe_regions = []
            for r in [params_region, cos_region, evt_region, es_region]:
                if r and r not in seen_r:
                    seen_r.add(r)
                    probe_regions.append(r)
            for r in ALL_INTL_REGIONS:
                if r not in seen_r:
                    seen_r.add(r)
                    probe_regions.append(r)

            for candidate in probe_regions:
                info = get_eip_by_instance_id(source_instance, candidate)
                if info:
                    eip_id = info.get("AddressId", "")
                    region = candidate
                    print(json.dumps({
                        "info": "transform_eip_discovered",
                        "eip_id": eip_id,
                        "source_instance": source_instance,
                        "region": candidate
                    }))
                    break

        if not eip_id:
            print(json.dumps({
                "warning": "transform_eip_not_found",
                "source_instance": source_instance,
                "regions_probed": len(probe_regions) if source_instance else 0
            }))
    else:
        # --- AllocateAddresses: try resourceSet first ---
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
            address_set = resp_elems.get("AddressSet", [])
            if address_set and isinstance(address_set, list):
                first_addr = address_set[0]
                if isinstance(first_addr, dict):
                    eip_id = _unwrap_id(first_addr.get("AddressId", ""))
                elif isinstance(first_addr, str):
                    eip_id = first_addr

    if not eip_id or not region:
        print(json.dumps({
            "warning": "eip_missing_id_or_region",
            "eip_id": eip_id,
            "region": region,
            "event": event_name
        }))
        return False

    # Query EIP details — try all candidate regions from CloudAudit record
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

    eip_info = None
    actual_region = region
    for candidate in candidates:
        eip_info = get_eip_info(eip_id, candidate)
        if eip_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "eip_found_in_alternate_region",
                    "eip_id": eip_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break

    region = actual_region

    eip_type = "EIP"
    linked_resource = ""

    if eip_info:
        eip_type = eip_info.get("AddressType", "EIP") or "EIP"
        linked_resource = eip_info.get("InstanceId", "") or ""
    else:
        print(json.dumps({
            "warning": "eip_info_unavailable",
            "eip_id": eip_id,
            "note": "tagging with defaults"
        }))

    # For TransformAddress, use the source CVM as linked_resource if not already set
    if event_name == "TransformAddress" and not linked_resource:
        linked_resource = req_params.get("InstanceId", "")

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
        "eip_type": eip_type,
        "linked_resource": linked_resource or "NONE"
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "eip_tagged",
            "eip_id": eip_id,
            "region": region,
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
