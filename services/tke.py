"""
TKE (Tencent Kubernetes Engine) Cluster Tagging Service

Handles tagging for:
- TKE clusters (CreateCluster)

TKE clusters fire a CloudAudit event under ResourceType "tke".
A dedicated tagger-tke-track monitors CreateCluster events.

CloudAudit quirks:
- resourceId may arrive as a stringified Python list: "['cls-xxx']"
- eventSource / eventRegion may point to a different region than where the
  cluster actually lives
- Region discovery: tries detected region first, then COS_REGION fallback

QCS format: qcs::tke:{region}:uin/{uin}:cluster/{cluster_id}
"""

import json
import os
import datetime
from typing import List, Dict, Any, Optional


def build_tke_tags(owner: str) -> List[Dict[str, str]]:
    """
    Build tags for TKE cluster resources.

    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCreated → TaggerOwner → TaggerProject → TaggerTTL

    Args:
        owner: Owner email/username

    Returns:
        List of tags to apply to TKE cluster
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",       "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",     "TagValue": today},
        {"TagKey": "TaggerCanDelete",   "TagValue": "YES"},
        {"TagKey": "TaggerTTL",         "TagValue": "3"},
        {"TagKey": "TaggerProject",     "TagValue": "n/a"},
    ]


def get_cluster_info(cluster_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query TKE cluster details using DescribeClusters API.

    Returns:
        Dict with keys: ClusterId, ClusterName, ClusterType, ClusterStatus,
                        ClusterNodeNum, ClusterVersion
        None if cluster not found or error
    """
    from index import make_tc_client
    from tencentcloud.tke.v20180525 import tke_client, models as tke_models

    client = make_tc_client("tke", tke_client.TkeClient, region)
    if not client:
        return None

    try:
        req = tke_models.DescribeClustersRequest()
        req.ClusterIds = [cluster_id]
        resp = client.DescribeClusters(req)

        clusters = getattr(resp, "Clusters", [])
        if not clusters:
            return None

        cluster = clusters[0]
        return {
            "ClusterId":      getattr(cluster, "ClusterId", ""),
            "ClusterName":    getattr(cluster, "ClusterName", ""),
            "ClusterType":    getattr(cluster, "ClusterType", ""),
            "ClusterStatus":  getattr(cluster, "ClusterStatus", ""),
            "ClusterNodeNum": getattr(cluster, "ClusterNodeNum", 0),
            "ClusterVersion": getattr(cluster, "ClusterVersion", ""),
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_cluster_info_failed",
            "cluster_id": cluster_id,
            "region": region,
            "message": str(e)
        }))
        return None


def handle_tke_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle TKE cluster tagging for CreateCluster events.

    Extraction strategy:
    1. Try resourceSet for cluster ID and region
    2. Fallback to responseElements.ClusterId
    3. Query cluster details for name and type info
    4. Build tags and apply via Tag API

    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs

    event_name = rec.get("eventName", "")
    if event_name != "CreateCluster":
        return False

    cluster_id = None
    region = extract_region(rec)

    def _unwrap_id(val):
        """Extract a plain string ID from a value that may be a list or string."""
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.startswith("cls-"):
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

    # Try resourceSet first
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list):
        for resource in resource_set:
            if not isinstance(resource, dict):
                continue
            raw_id = resource.get("resourceId", "")
            unwrapped = _unwrap_id(raw_id)
            if unwrapped and unwrapped.startswith("cls-"):
                cluster_id = unwrapped
                if not region:
                    region = resource.get("resourceRegion")
                break
        if not cluster_id and resource_set:
            first = resource_set[0]
            if isinstance(first, dict):
                cluster_id = _unwrap_id(first.get("resourceId"))
                if not region:
                    region = first.get("resourceRegion")

    # Fallback: parse responseElements for ClusterId
    if not cluster_id:
        resp_elems = _safe_dict(rec.get("responseElements"))
        cluster_id = _unwrap_id(resp_elems.get("ClusterId", ""))

    if not cluster_id or not region:
        print(json.dumps({
            "warning": "tke_missing_id_or_region",
            "cluster_id": cluster_id,
            "region": region,
            "event": event_name
        }))
        return False

    # Query cluster details — try candidate regions
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

    cluster_info = None
    actual_region = region
    for candidate in candidates:
        cluster_info = get_cluster_info(cluster_id, candidate)
        if cluster_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "tke_found_in_alternate_region",
                    "cluster_id": cluster_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break
        if candidate != candidates[-1]:
            print(json.dumps({
                "info": "tke_region_retry",
                "cluster_id": cluster_id,
                "tried": candidate,
                "trying": candidates[candidates.index(candidate) + 1],
                "reason": "cluster_not_found_in_region"
            }))

    region = actual_region

    cluster_name = ""
    cluster_type = ""
    if cluster_info:
        cluster_name = cluster_info.get("ClusterName", "") or ""
        cluster_type = cluster_info.get("ClusterType", "") or ""
        print(json.dumps({
            "info": "tke_details",
            "cluster_id": cluster_id,
            "status": cluster_info.get("ClusterStatus", ""),
            "cluster_name": cluster_name,
            "cluster_type": cluster_type,
            "node_count": cluster_info.get("ClusterNodeNum", 0),
            "version": cluster_info.get("ClusterVersion", "")
        }))
    else:
        # Fallback: extract from requestParameters
        req_raw = rec.get("requestParameters", {})
        if isinstance(req_raw, str):
            try:
                req_raw = json.loads(req_raw)
            except Exception:
                req_raw = {}
        if isinstance(req_raw, dict):
            cluster_type = req_raw.get("ClusterType", "") or ""
            basic = req_raw.get("ClusterBasicSettings", {})
            if isinstance(basic, dict):
                cluster_name = basic.get("ClusterName", "") or ""
        print(json.dumps({
            "warning": "tke_info_unavailable",
            "cluster_id": cluster_id,
            "note": "tagging with defaults from requestParameters"
        }))

    # Build QCS for TKE cluster
    # qcs::tke:{region}:uin/{uin}:cluster/{cluster_id}
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::tke:{region}:uin/{account_uin}:cluster/{cluster_id}"

    owner = get_owner(rec)
    tags = build_tke_tags(owner=owner)

    print(json.dumps({
        "info": "tke_tagging",
        "cluster_id": cluster_id,
        "region": region,
        "qcs": qcs
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "tke_tagged",
            "cluster_id": cluster_id,
            "qcs": qcs,
            "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({
            "error": "tke_tagging_failed",
            "cluster_id": cluster_id,
            "qcs": qcs,
            "message": str(e)
        }))
        return False
