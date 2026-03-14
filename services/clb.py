"""
CLB (Cloud Load Balancer) Tagging Service

Handles tagging for:
- CLB instances (CreateLoadBalancer)

CLBs can only be created or deleted (no start/stop operations).

QCS format: qcs::clb:{region}:uin/{uin}:clb/{lb_id}
"""

import json
import datetime
from typing import List, Dict, Any


def build_clb_tags(owner: str) -> List[Dict[str, str]]:
    """
    Build tags for CLB resources.
    
    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCreated → TaggerOwner → TaggerProject → TaggerTTL
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",     "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",   "TagValue": today},
        {"TagKey": "TaggerCanDelete", "TagValue": "YES"},
        {"TagKey": "TaggerTTL",       "TagValue": "7"},
        {"TagKey": "TaggerProject",   "TagValue": "n/a"},
    ]


def handle_clb_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle CLB tagging for CreateLoadBalancer events.
    
    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs

    event_name = rec.get("eventName", "")
    if event_name != "CreateLoadBalancer":
        return False

    # Extract LB ID and region
    lb_id = None
    region = None

    # Try resourceSet first
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list) and len(resource_set) > 0:
        first_resource = resource_set[0]
        if isinstance(first_resource, dict):
            lb_id_raw = first_resource.get("resourceId")
            if lb_id_raw and isinstance(lb_id_raw, str):
                if lb_id_raw.startswith("[") and lb_id_raw.endswith("]"):
                    try:
                        lb_id_json = lb_id_raw.replace("'", '"')
                        lb_ids_list = json.loads(lb_id_json)
                        if lb_ids_list and isinstance(lb_ids_list, list):
                            lb_id = lb_ids_list[0]
                        else:
                            lb_id = lb_id_raw
                    except Exception:
                        lb_id = lb_id_raw
                else:
                    lb_id = lb_id_raw
            else:
                lb_id = lb_id_raw
            region = first_resource.get("resourceRegion")

    if not region:
        region = extract_region(rec)

    # Fallback: parse responseElements for LoadBalancerIds
    if not lb_id:
        resp_str = rec.get("responseElements", "")
        if resp_str:
            try:
                resp = json.loads(resp_str)
                lb_ids = resp.get("LoadBalancerIds", [])
                if lb_ids:
                    lb_id = lb_ids[0]
            except Exception:
                pass

    if not lb_id or not region:
        print(json.dumps({
            "warning": "clb_missing_id_or_region",
            "lb_id": lb_id,
            "region": region
        }))
        return False

    account_uin = extract_account_uin(rec)
    qcs = f"qcs::clb:{region}:uin/{account_uin}:clb/{lb_id}"

    print(json.dumps({
        "info": "clb_tagging",
        "lb_id": lb_id,
        "region": region,
        "qcs": qcs
    }))

    owner = get_owner(rec)
    tags = build_clb_tags(owner)

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "clb_tagged",
            "lb_id": lb_id,
            "qcs": qcs,
            "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({
            "error": "clb_tagging_failed",
            "lb_id": lb_id,
            "qcs": qcs,
            "message": str(e)
        }))
        return False
