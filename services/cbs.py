"""
CBS (Cloud Block Storage) Tagging Service

Handles tagging for:
- CBS disks (CreateCbsStorages, CreateDisks, AttachDisks)

Strategy:
- Attached disks: Copy TaggerProject from CVM, recreate other tags
- Unattached disks: Apply default tags with TaggerProject="n/a"

QCS format: qcs::cvm:{region}:uin/{uin}:volume/{disk_id}
"""

import json
import re
import time
import datetime
from typing import List, Dict, Any, Optional


def build_cbs_tags(owner: str, disk_usage: str = "SYSTEM", linked_cvm: bool = False, cvm_project: str = "") -> List[Dict[str, str]]:
    """
    Build tags for CBS disks.
    
    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCreated → TaggerLinkedCVM → TaggerOwner → 
    TaggerProject → TaggerTTL → TaggerUsage
    """
    today = datetime.date.today().isoformat()
    tags = [
        {"TagKey": "TaggerOwner",     "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",   "TagValue": today},
        {"TagKey": "TaggerUsage",     "TagValue": disk_usage.upper()},
        {"TagKey": "TaggerLinkedCVM", "TagValue": "YES" if linked_cvm else "NO"},
        {"TagKey": "TaggerCanDelete", "TagValue": "YES"},
        {"TagKey": "TaggerTTL",       "TagValue": "7"},
        {"TagKey": "TaggerProject",   "TagValue": cvm_project or "n/a"},
    ]
    return tags


def parse_disk_usage(disk_usage: str) -> str:
    """
    Convert CBS DiskUsage API field to tag value.
    
    Returns:
        SYSTEM or DATA
    """
    if "SYSTEM" in disk_usage.upper():
        return "SYSTEM"
    return "DATA"


def get_disk_info(disk_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query CBS disk details using DescribeDisks API.
    """
    from index import make_tc_client
    from tencentcloud.cbs.v20170312 import cbs_client, models as cbs_models

    client = make_tc_client("cbs", cbs_client.CbsClient, region)
    if not client:
        return None

    try:
        req = cbs_models.DescribeDisksRequest()
        req.DiskIds = [disk_id]
        resp = client.DescribeDisks(req)

        disks = getattr(resp, "DiskSet", [])
        if not disks:
            return None

        disk = disks[0]
        return {
            "DiskState":   getattr(disk, "DiskState", ""),
            "InstanceId":  getattr(disk, "InstanceId", ""),
            "DiskUsage":   getattr(disk, "DiskUsage", "DATA_DISK"),
            "CreateTime":  getattr(disk, "CreateTime", ""),
        }
    except Exception as e:
        print(json.dumps({"error": "get_disk_info_failed", "disk_id": disk_id, "region": region, "message": str(e)}))
        return None


def get_cvm_tags(instance_id: str, region: str) -> Dict[str, str]:
    """
    Query tags from a CVM instance.
    """
    from index import make_tag_client
    from tencentcloud.tag.v20180813 import models as tag_models

    client = make_tag_client(region)
    try:
        req = tag_models.DescribeResourceTagsByResourceIdsRequest()
        req.ServiceType = "cvm"
        req.ResourcePrefix = "instance"
        req.ResourceIds = [instance_id]
        req.ResourceRegion = region

        resp = client.DescribeResourceTagsByResourceIds(req)
        tag_list = getattr(resp, "Tags", [])

        tags = {}
        for tag in tag_list:
            key = getattr(tag, "TagKey", "")
            value = getattr(tag, "TagValue", "")
            if key:
                tags[key] = value

        return tags
    except Exception as e:
        print(json.dumps({"error": "get_cvm_tags_failed", "instance_id": instance_id, "region": region, "message": str(e)}))
        return {}


def find_recent_disk_with_retry(region: str, event_time: int, window_seconds: int = 300,
                                  max_retries: int = 4, delays: Optional[List[int]] = None) -> Optional[str]:
    """
    Find most recently created disk with retry logic for timing issues.
    """
    if delays is None:
        delays = [10, 20, 30, 40]

    for attempt in range(max_retries + 1):
        disk_id = find_recent_disk(region, event_time, window_seconds)

        if disk_id:
            if attempt > 0:
                print(json.dumps({
                    "info": "cbs_retry_succeeded",
                    "attempt": attempt + 1,
                    "disk_id": disk_id
                }))
            return disk_id

        if attempt < max_retries:
            delay = delays[attempt] if attempt < len(delays) else delays[-1]
            print(json.dumps({
                "info": "cbs_disk_not_found_retrying",
                "attempt": attempt + 1,
                "delay_seconds": delay,
                "reason": "disk_provisioning_may_be_in_progress"
            }))
            time.sleep(delay)

    print(json.dumps({
        "warning": "cbs_disk_not_found_after_retries",
        "attempts": max_retries + 1,
        "total_wait_seconds": sum(delays[:max_retries]),
        "note": "disk may need manual tagging or CloudAudit event arrived too early"
    }))
    return None


def find_recent_disk(region: str, event_time: int, window_seconds: int = 300) -> Optional[str]:
    """
    Find most recently created untagged disk in region within time window.
    """
    from index import make_tc_client
    from tencentcloud.cbs.v20170312 import cbs_client, models as cbs_models

    client = make_tc_client("cbs", cbs_client.CbsClient, region)
    if not client:
        return None

    try:
        req = cbs_models.DescribeDisksRequest()
        req.Limit = 20
        req.Order = "DESC"
        req.OrderField = "CREATE_TIME"
        resp = client.DescribeDisks(req)

        disks = getattr(resp, "DiskSet", [])
        if not disks:
            return None

        event_dt = datetime.datetime.fromtimestamp(event_time)

        for disk in disks:
            create_time_str = getattr(disk, "CreateTime", "")
            if not create_time_str:
                continue

            try:
                create_dt = datetime.datetime.strptime(create_time_str, "%Y-%m-%d %H:%M:%S")
                time_diff = abs((create_dt - event_dt).total_seconds())

                if time_diff <= window_seconds:
                    disk_id = getattr(disk, "DiskId", "")

                    disk_tags = getattr(disk, "Tags", []) or []
                    has_tagger = any(
                        getattr(t, "Key", "") == "TaggerOwner" or getattr(t, "TagKey", "") == "TaggerOwner"
                        for t in disk_tags
                    )
                    if has_tagger:
                        continue

                    print(json.dumps({
                        "info": "found_recent_disk",
                        "disk_id": disk_id,
                        "create_time": create_time_str
                    }))
                    return disk_id
            except Exception:
                continue

        return None
    except Exception as e:
        print(json.dumps({"error": "find_recent_disk_failed", "region": region, "message": str(e)}))
        return None


def handle_cbs_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle CBS disk tagging for CreateCbsStorages, CreateDisks, and AttachDisks events.
    
    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs

    event_name = rec.get("eventName", "")
    if event_name not in ("CreateCbsStorages", "CreateDisks", "AttachDisks"):
        return False

    disk_id = None
    region = extract_region(rec)

    # Try resourceSet first
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list) and len(resource_set) > 0:
        first_resource = resource_set[0]
        if isinstance(first_resource, dict):
            disk_id_raw = first_resource.get("resourceId")
            if disk_id_raw and isinstance(disk_id_raw, str):
                if disk_id_raw.startswith("[") and disk_id_raw.endswith("]"):
                    try:
                        parsed = json.loads(disk_id_raw.replace("'", '"'))
                        if isinstance(parsed, list) and parsed:
                            disk_id = parsed[0]
                        else:
                            disk_id = disk_id_raw
                    except Exception:
                        m = re.search(r"(disk-[a-zA-Z0-9]+)", disk_id_raw)
                        disk_id = m.group(1) if m else disk_id_raw
                else:
                    disk_id = disk_id_raw
            else:
                disk_id = disk_id_raw
            if not region:
                region = first_resource.get("resourceRegion")

    # Try responseElements fallback
    if not disk_id:
        resp_str = rec.get("responseElements", "")
        if resp_str and "DiskIdSet" in resp_str:
            try:
                resp = json.loads(resp_str)
                disk_ids = resp.get("DiskIdSet", [])
                if disk_ids:
                    disk_id = disk_ids[0]
            except Exception:
                pass

    # For AttachDisks, try requestParameters
    if not disk_id and event_name == "AttachDisks":
        req_params_raw = rec.get("requestParameters", {})
        if isinstance(req_params_raw, str):
            try:
                req_params = json.loads(req_params_raw)
            except Exception:
                req_params = {}
        else:
            req_params = req_params_raw if isinstance(req_params_raw, dict) else {}
        disk_ids = req_params.get("DiskIds", [])
        if disk_ids:
            disk_id = disk_ids[0]

    # For CreateCbsStorages/CreateDisks, query CBS for recent disks
    if not disk_id and event_name in ("CreateCbsStorages", "CreateDisks"):
        if region:
            disk_id = find_recent_disk_with_retry(region, rec.get("eventTime", 0))
            if not disk_id:
                print(json.dumps({
                    "warning": "no_recent_disk_found_after_retry",
                    "region": region
                }))
                return False
        else:
            print(json.dumps({"error": "cbs_no_region", "event": event_name}))
            return False

    if not disk_id or not region:
        print(json.dumps({
            "error": "cbs_tagging_failed",
            "reason": "missing_disk_id_or_region",
            "event": event_name,
            "disk_id": disk_id,
            "region": region
        }))
        return False

    disk_info = get_disk_info(disk_id, region)
    if not disk_info:
        print(json.dumps({"error": "cbs_tagging_skipped", "reason": "disk_info_not_available", "disk_id": disk_id}))
        return False

    disk_state = disk_info.get("DiskState", "")
    instance_id = disk_info.get("InstanceId", "")
    disk_usage = disk_info.get("DiskUsage", "DATA_DISK")

    owner = get_owner(rec)

    if disk_state == "ATTACHED" and instance_id:
        cvm_tags = get_cvm_tags(instance_id, region)
        cvm_project = cvm_tags.get("TaggerProject", "")

        tags = build_cbs_tags(
            owner=owner,
            disk_usage=parse_disk_usage(disk_usage),
            linked_cvm=True,
            cvm_project=cvm_project
        )

        print(json.dumps({
            "info": "cbs_tagging_attached",
            "disk_id": disk_id,
            "instance_id": instance_id,
            "project": cvm_project or "(empty)"
        }))
    else:
        tags = build_cbs_tags(
            owner=owner,
            disk_usage=parse_disk_usage(disk_usage),
            linked_cvm=False,
            cvm_project=""
        )

        print(json.dumps({
            "info": "cbs_tagging_unattached",
            "disk_id": disk_id,
            "disk_state": disk_state
        }))

    owner_uin = extract_account_uin(rec)
    qcs = f"qcs::cvm:{region}:uin/{owner_uin}:volume/{disk_id}"

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "cbs_tagged",
            "disk_id": disk_id,
            "region": region,
            "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({"error": "cbs_tagging_failed", "disk_id": disk_id, "qcs": qcs, "message": str(e)}))
        return False
