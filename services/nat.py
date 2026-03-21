"""
NAT Gateway Tagging Service

Handles tagging for:
- Public NAT Gateway instances (CreateNatGateway)
- Private NAT Gateway instances (CreatePrivateNatGateway)
- EIPs auto-allocated by public NAT Gateway creation

Both public and private NAT Gateways fire CloudAudit events under
ResourceType "vpc" (same track as EIP/ENI/HAVIP).

Public NAT (nat-xxx):
  When created, auto-allocates one or more EIPs. The corresponding
  AllocateAddresses CA event fires under ResourceType "cvm" with empty
  requestParameters/responseElements/resourceSet, making it impossible
  for the EIP handler to discover the EIP ID. The NAT handler queries
  the NAT gateway's PublicIpAddressSet via DescribeNatGateways and tags
  each associated EIP directly.

Private NAT (intranat-xxx):
  Used for VPC-to-VPC or VPC-to-CCN traffic. No public EIPs are involved.
  Details queried via DescribePrivateNatGateways API.

CloudAudit quirks:
- resourceId may arrive as a stringified Python list: "['nat-xxx']"
- eventSource / eventRegion may point to a different region than where the
  NAT gateway actually lives
- Region discovery: tries detected region first, then COS_REGION fallback
- Private NAT: resourceSet is empty; ID comes from responseElements

QCS formats:
  Public:  qcs::vpc:{region}:uin/{uin}:nat/{nat_id}
  Private: qcs::vpc:{region}:uin/{uin}:intranat/{intranat_id}
"""

import json
import os
import datetime
from typing import List, Dict, Any, Optional


def build_nat_tags(owner: str) -> List[Dict[str, str]]:
    """
    Build tags for NAT Gateway resources (public and private).

    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCreated → TaggerOwner →
    TaggerProject → TaggerTTL

    Args:
        owner: Owner email/username

    Returns:
        List of tags to apply to NAT gateway
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",     "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",   "TagValue": today},
        {"TagKey": "TaggerCanDelete", "TagValue": "YES"},
        {"TagKey": "TaggerTTL",       "TagValue": "3"},
        {"TagKey": "TaggerProject",   "TagValue": "n/a"},
    ]


def get_nat_info(nat_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query public NAT Gateway details using DescribeNatGateways API.

    Returns:
        Dict with keys: NatGatewayId, NatGatewayName, VpcId, State,
                        MaxConcurrent, Bandwidth, EipIds
        None if NAT gateway not found or error
    """
    from index import make_tc_client
    from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models

    client = make_tc_client("vpc", vpc_client.VpcClient, region)
    if not client:
        return None

    try:
        req = vpc_models.DescribeNatGatewaysRequest()
        req.NatGatewayIds = [nat_id]
        resp = client.DescribeNatGateways(req)

        nats = getattr(resp, "NatGatewaySet", [])
        if not nats:
            return None

        nat = nats[0]
        # Extract public IP addresses (EIPs bound to this NAT gateway)
        pub_ips = getattr(nat, "PublicIpAddressSet", []) or []
        eip_ids = []
        for ip_obj in pub_ips:
            aid = getattr(ip_obj, "AddressId", "") if hasattr(ip_obj, "AddressId") else (ip_obj.get("AddressId", "") if isinstance(ip_obj, dict) else "")
            if aid:
                eip_ids.append(aid)
        return {
            "NatGatewayId":   getattr(nat, "NatGatewayId", ""),
            "NatGatewayName": getattr(nat, "NatGatewayName", ""),
            "VpcId":          getattr(nat, "VpcId", ""),
            "State":          getattr(nat, "State", ""),
            "MaxConcurrent":  getattr(nat, "MaxConcurrent", 0),
            "Bandwidth":      getattr(nat, "Bandwidth", 0),
            "EipIds":         eip_ids,
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_nat_info_failed",
            "nat_id": nat_id,
            "region": region,
            "message": str(e)
        }))
        return None


def get_private_nat_info(nat_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query private NAT Gateway details using DescribePrivateNatGateways API.

    Returns:
        Dict with keys: NatGatewayId, NatGatewayName, VpcId, Status,
                        NatType, CcnId, CrossDomain
        None if private NAT gateway not found or error
    """
    from index import make_tc_client
    from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models

    client = make_tc_client("vpc", vpc_client.VpcClient, region)
    if not client:
        return None

    try:
        req = vpc_models.DescribePrivateNatGatewaysRequest()
        req.NatGatewayIds = [nat_id]
        resp = client.DescribePrivateNatGateways(req)

        nats = getattr(resp, "PrivateNatGatewaySet", [])
        if not nats:
            return None

        nat = nats[0]
        return {
            "NatGatewayId":   getattr(nat, "NatGatewayId", ""),
            "NatGatewayName": getattr(nat, "NatGatewayName", ""),
            "VpcId":          getattr(nat, "VpcId", ""),
            "Status":         getattr(nat, "Status", ""),
            "NatType":        getattr(nat, "NatType", ""),
            "CcnId":          getattr(nat, "CcnId", ""),
            "CrossDomain":    getattr(nat, "CrossDomain", False),
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_private_nat_info_failed",
            "nat_id": nat_id,
            "region": region,
            "message": str(e)
        }))
        return None


def _resolve_nat_region(rec, resource_set, req_raw):
    """
    Resolve NAT gateway region from CA record.
    Priority: resourceSet.resourceRegion > requestParameters.Region > eventRegion > eventSource
    """
    from index import extract_region

    region = None
    if resource_set and isinstance(resource_set, list):
        for resource in resource_set:
            if isinstance(resource, dict):
                rr = resource.get("resourceRegion")
                if rr:
                    region = rr
                    break
    if not region:
        region = req_raw.get("Region") or ""
    if not region:
        region = rec.get("eventRegion") or ""
    if not region:
        region = extract_region(rec) or ""
    return region


def _build_region_candidates(region, rec):
    """Build ordered list of candidate regions for API probing."""
    from index import _region_from_event_source
    cos_region = os.getenv("COS_REGION", "")
    event_source_region = _region_from_event_source(rec.get("eventSource", ""))

    seen = set()
    candidates = []
    for r in [region, cos_region, event_source_region]:
        if r and r not in seen:
            seen.add(r)
            candidates.append(r)
    return candidates


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


def _unwrap_id(val, prefix="nat-"):
    """Extract a plain string ID from a value that may be a list or string."""
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str) and item.startswith(prefix):
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


def handle_nat_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle NAT Gateway tagging for CreateNatGateway and CreatePrivateNatGateway.

    Dispatches to the appropriate handler based on eventName.

    Returns:
        True if tagging succeeded, False otherwise
    """
    event_name = rec.get("eventName", "")
    if event_name == "CreateNatGateway":
        return _handle_public_nat(rec)
    if event_name == "CreatePrivateNatGateway":
        return _handle_private_nat(rec)
    return False


def _handle_public_nat(rec: Dict[str, Any]) -> bool:
    """Handle tagging for public NAT Gateway (CreateNatGateway)."""
    from index import extract_account_uin, get_owner, tag_resource_qcs

    # Parse requestParameters and responseElements
    req_raw = rec.get("requestParameters", {})
    if isinstance(req_raw, str):
        try:
            req_raw = json.loads(req_raw)
        except Exception:
            req_raw = {}
    if not isinstance(req_raw, dict):
        req_raw = {}

    resp_elems = _safe_dict(rec.get("responseElements"))

    # Extract NAT ID from resourceSet
    nat_id = None
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list):
        for resource in resource_set:
            if not isinstance(resource, dict):
                continue
            raw_id = resource.get("resourceId", "")
            unwrapped = _unwrap_id(raw_id, "nat-")
            if unwrapped and unwrapped.startswith("nat-"):
                nat_id = unwrapped
                break

    # Fallback: parse responseElements
    if not nat_id:
        nat_gw_list = resp_elems.get("NatGatewaySet") or resp_elems.get("NatGateway") or []
        if isinstance(nat_gw_list, list) and nat_gw_list:
            first_nat = nat_gw_list[0]
            if isinstance(first_nat, dict):
                nat_id = _unwrap_id(first_nat.get("NatGatewayId", ""), "nat-")
        elif isinstance(nat_gw_list, dict):
            nat_id = _unwrap_id(nat_gw_list.get("NatGatewayId", ""), "nat-")
        if not nat_id:
            nat_id = _unwrap_id(resp_elems.get("NatGatewayId", ""), "nat-")

    region = _resolve_nat_region(rec, resource_set, req_raw)

    if not nat_id or not region:
        print(json.dumps({
            "warning": "nat_missing_id_or_region",
            "nat_id": nat_id, "region": region, "event": "CreateNatGateway"
        }))
        return False

    # Query NAT gateway details — try candidate regions
    candidates = _build_region_candidates(region, rec)

    nat_info = None
    actual_region = region
    for candidate in candidates:
        nat_info = get_nat_info(nat_id, candidate)
        if nat_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "nat_found_in_alternate_region",
                    "nat_id": nat_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break

    region = actual_region

    if nat_info:
        print(json.dumps({
            "info": "nat_details",
            "nat_id": nat_id,
            "state": nat_info.get("State", ""),
            "nat_name": nat_info.get("NatGatewayName", ""),
            "vpc_id": nat_info.get("VpcId", ""),
            "bandwidth": nat_info.get("Bandwidth", 0)
        }))
    else:
        print(json.dumps({
            "warning": "nat_info_unavailable",
            "nat_id": nat_id,
            "note": "tagging with defaults from request/response params"
        }))

    # Build QCS and tags
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::vpc:{region}:uin/{account_uin}:nat/{nat_id}"
    owner = get_owner(rec)
    tags = build_nat_tags(owner=owner)

    print(json.dumps({
        "info": "nat_tagging", "nat_id": nat_id, "region": region, "qcs": qcs
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "nat_tagged", "nat_id": nat_id, "qcs": qcs, "tags_applied": len(tags)
        }))
    except Exception as e:
        print(json.dumps({
            "error": "nat_tagging_failed", "nat_id": nat_id, "qcs": qcs, "message": str(e)
        }))
        return False

    # --- Also tag EIPs allocated by this NAT gateway ---
    eip_ids = nat_info.get("EipIds", []) if nat_info else []
    if eip_ids:
        from services.eip import build_eip_tags, get_eip_info
        for eip_id in eip_ids:
            try:
                eip_info = get_eip_info(eip_id, region)
                eip_type = eip_info.get("AddressType", "EIP") if eip_info else "EIP"
                linked = nat_id
                eip_qcs = f"qcs::cvm:{region}:uin/{account_uin}:eip/{eip_id}"
                eip_tags = build_eip_tags(owner=owner, eip_type=eip_type, linked_resource=linked)
                tag_resource_qcs(region, eip_qcs, eip_tags)
                print(json.dumps({
                    "success": "nat_eip_tagged",
                    "eip_id": eip_id, "nat_id": nat_id,
                    "qcs": eip_qcs, "tags_applied": len(eip_tags)
                }))
            except Exception as e:
                print(json.dumps({
                    "error": "nat_eip_tagging_failed",
                    "eip_id": eip_id, "nat_id": nat_id, "message": str(e)
                }))
    else:
        print(json.dumps({
            "info": "nat_no_eips_to_tag",
            "nat_id": nat_id,
            "note": "NAT gateway has no associated EIPs or info unavailable"
        }))

    return True


def _handle_private_nat(rec: Dict[str, Any]) -> bool:
    """Handle tagging for private NAT Gateway (CreatePrivateNatGateway)."""
    from index import extract_account_uin, get_owner, tag_resource_qcs

    # Parse requestParameters and responseElements
    req_raw = rec.get("requestParameters", {})
    if isinstance(req_raw, str):
        try:
            req_raw = json.loads(req_raw)
        except Exception:
            req_raw = {}
    if not isinstance(req_raw, dict):
        req_raw = {}

    resp_elems = _safe_dict(rec.get("responseElements"))

    # Extract private NAT ID — resourceSet is typically empty for private NAT
    nat_id = None
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list):
        for resource in resource_set:
            if not isinstance(resource, dict):
                continue
            raw_id = resource.get("resourceId", "")
            unwrapped = _unwrap_id(raw_id, "intranat-")
            if unwrapped and unwrapped.startswith("intranat-"):
                nat_id = unwrapped
                break

    # Fallback: parse responseElements.PrivateNatGatewaySet
    if not nat_id:
        nat_gw_list = resp_elems.get("PrivateNatGatewaySet") or []
        if isinstance(nat_gw_list, list) and nat_gw_list:
            first_nat = nat_gw_list[0]
            if isinstance(first_nat, dict):
                nat_id = first_nat.get("NatGatewayId", "")
        if not nat_id:
            nat_id = resp_elems.get("NatGatewayId", "")

    region = _resolve_nat_region(rec, resource_set, req_raw)

    if not nat_id or not region:
        print(json.dumps({
            "warning": "private_nat_missing_id_or_region",
            "nat_id": nat_id, "region": region, "event": "CreatePrivateNatGateway"
        }))
        return False

    # Query private NAT gateway details — try candidate regions
    candidates = _build_region_candidates(region, rec)

    nat_info = None
    actual_region = region
    for candidate in candidates:
        nat_info = get_private_nat_info(nat_id, candidate)
        if nat_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "private_nat_found_in_alternate_region",
                    "nat_id": nat_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break

    region = actual_region

    if nat_info:
        print(json.dumps({
            "info": "private_nat_details",
            "nat_id": nat_id,
            "status": nat_info.get("Status", ""),
            "nat_name": nat_info.get("NatGatewayName", ""),
            "nat_type": nat_info.get("NatType", ""),
            "vpc_id": nat_info.get("VpcId", ""),
            "ccn_id": nat_info.get("CcnId", ""),
        }))
    else:
        # Use responseElements as fallback
        nat_name = req_raw.get("NatGatewayName", "") or ""
        nat_gw_list = resp_elems.get("PrivateNatGatewaySet") or []
        if isinstance(nat_gw_list, list) and nat_gw_list:
            first_nat = nat_gw_list[0]
            if isinstance(first_nat, dict):
                nat_name = nat_name or first_nat.get("NatGatewayName", "") or ""
        print(json.dumps({
            "warning": "private_nat_info_unavailable",
            "nat_id": nat_id,
            "note": "tagging with defaults from request/response params"
        }))

    # Build QCS — private NAT uses 'intranat' resource type:
    # qcs::vpc:{region}:uin/{uin}:intranat/{intranat_id}
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::vpc:{region}:uin/{account_uin}:intranat/{nat_id}"
    owner = get_owner(rec)
    tags = build_nat_tags(owner=owner)

    print(json.dumps({
        "info": "private_nat_tagging", "nat_id": nat_id, "region": region, "qcs": qcs
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "private_nat_tagged",
            "nat_id": nat_id, "qcs": qcs, "tags_applied": len(tags)
        }))
    except Exception as e:
        print(json.dumps({
            "error": "private_nat_tagging_failed",
            "nat_id": nat_id, "qcs": qcs, "message": str(e)
        }))
        return False

    return True
