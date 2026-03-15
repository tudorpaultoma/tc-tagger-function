"""
CVM/CDH Tagging Service

Handles tagging for:
- CVM instances (RunInstances)
- CDH dedicated hosts (AllocateHosts)
- CBS disks attached to newly created CVM instances

QCS formats:
- CVM: qcs::cvm:{region}:uin/{uin}:instance/{instance_id}
- CDH: qcs::cvm:{region}:uin/{uin}:host/{host_id}
"""

import json
import time
import datetime
from typing import List, Dict, Any, Optional


def build_tags(owner: str) -> List[Dict[str, str]]:
    """
    Build standardized tags for CVM/CDH resources.
    
    Tags (displayed alphabetically in console):
    TaggerAutoOff → TaggerAutoStart → TaggerCanDelete → TaggerCreated → 
    TaggerOwner → TaggerProject → TaggerTTL
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",     "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",   "TagValue": today},
        {"TagKey": "TaggerAutoOff",   "TagValue": "YES"},
        {"TagKey": "TaggerAutoStart", "TagValue": "NO"},
        {"TagKey": "TaggerCanDelete", "TagValue": "YES"},
        {"TagKey": "TaggerTTL",       "TagValue": "3"},
        {"TagKey": "TaggerProject",   "TagValue": "n/a"},
    ]


def extract_qcs(rec: Dict[str, Any]) -> Optional[str]:
    """
    Build QCS identifier for CVM instance or CDH creation events.
    Supports:
    - CVM RunInstances events -> qcs::cvm:{region}:uin/{uin}:instance/{instance_id}
    - CDH AllocateHosts events -> qcs::cvm:{region}:uin/{uin}:host/{host_id}
    """
    from index import extract_region

    # Direct QCS field?
    for key in ("resourceId", "resource", "resourceQcs", "qcs", "targetResource"):
        val = rec.get(key)
        if isinstance(val, str) and val.startswith("qcs::"):
            return val

    # Handle CVM RunInstances events
    evt = rec.get("eventName", "")
    if evt == "RunInstances":
        # Try resourceSet first - but filter for actual instance (not keypair/sg/vpc/etc)
        resource_set = rec.get("resourceSet", [])
        if resource_set:
            for resource in resource_set:
                if not isinstance(resource, dict):
                    continue
                resource_type_class = resource.get("resourceTypeClass", "")
                if "Instance" in resource_type_class and "Keypair" not in resource_type_class:
                    instance_id     = _unwrap_resource_id(resource.get("resourceId"))
                    resource_region = resource.get("resourceRegion")
                    user_id         = rec.get("userIdentity", {})
                    if isinstance(user_id, dict):
                        owner_uin = user_id.get("accountId") or user_id.get("principalId") or user_id.get("ownerUin") or ""
                    else:
                        owner_uin = ""
                    if instance_id and resource_region:
                        return f"qcs::cvm:{resource_region}:uin/{owner_uin}:instance/{instance_id}"
                    break

        # Fallback: parse responseElements
        resp_str = rec.get("responseElements", "")
        if resp_str and "InstanceIdSet" in resp_str:
            try:
                resp = json.loads(resp_str)
                ids  = resp.get("InstanceIdSet", [])
                if ids:
                    instance_id = ids[0]
                    resource_region = _extract_region_from_params(rec)
                    if not resource_region:
                        resource_region = extract_region(rec)
                    user_id = rec.get("userIdentity", {})
                    if isinstance(user_id, dict):
                        owner_uin = user_id.get("accountId") or user_id.get("principalId") or user_id.get("ownerUin") or ""
                    else:
                        owner_uin = ""
                    if instance_id and resource_region:
                        part = f"uin/{owner_uin}" if owner_uin else ""
                        return f"qcs::cvm:{resource_region}:{part}:instance/{instance_id}"
            except Exception:
                pass

    # Handle CDH AllocateHosts events
    if evt == "AllocateHosts":
        resource_set = rec.get("resourceSet", [])
        if resource_set:
            for resource in resource_set:
                if not isinstance(resource, dict):
                    continue
                resource_type_class = resource.get("resourceTypeClass", "")
                if "Host" in resource_type_class:
                    host_id         = _unwrap_resource_id(resource.get("resourceId"))
                    resource_region = resource.get("resourceRegion")
                    user_id         = rec.get("userIdentity", {})
                    if isinstance(user_id, dict):
                        owner_uin = user_id.get("accountId") or user_id.get("principalId") or user_id.get("ownerUin") or ""
                    else:
                        owner_uin = ""
                    if host_id and resource_region:
                        return f"qcs::cvm:{resource_region}:uin/{owner_uin}:host/{host_id}"
                    break

        resp_str = rec.get("responseElements", "")
        if resp_str and "HostIdSet" in resp_str:
            try:
                resp = json.loads(resp_str)
                ids  = resp.get("HostIdSet", [])
                if ids:
                    host_id = ids[0]
                    resource_region = _extract_region_from_params(rec)
                    if not resource_region:
                        resource_region = extract_region(rec)
                    user_id = rec.get("userIdentity", {})
                    if isinstance(user_id, dict):
                        owner_uin = user_id.get("accountId") or user_id.get("principalId") or user_id.get("ownerUin") or ""
                    else:
                        owner_uin = ""
                    if host_id and resource_region:
                        part = f"uin/{owner_uin}" if owner_uin else ""
                        return f"qcs::cvm:{resource_region}:{part}:host/{host_id}"
            except Exception:
                pass

    # Generic fallback
    from index import extract_region as _er
    service = rec.get("service") or rec.get("eventSource")
    params_raw = rec.get("requestParameters", {})
    if isinstance(params_raw, str):
        try:
            params = json.loads(params_raw)
        except Exception:
            params = {}
    else:
        params = params_raw if isinstance(params_raw, dict) else {}

    rid     = rec.get("resourceId") or params.get("ResourceId")
    region  = _er(rec)
    if service and rid and region:
        ui = rec.get("userIdentity", {})
        if isinstance(ui, dict):
            owner = ui.get("accountId") or ui.get("principalId") or ui.get("ownerUin") or ""
        else:
            owner = ""
        upart = f"uin/{owner}" if owner else ""
        return f"qcs::{service}:{region}:{upart}:resourceId/{rid}"

    return None


def _extract_region_from_params(rec: Dict[str, Any]) -> Optional[str]:
    """Extract region from requestParameters Placement.Zone."""
    req_params_raw = rec.get("requestParameters", {})
    if isinstance(req_params_raw, str):
        try:
            req_params = json.loads(req_params_raw)
        except Exception:
            return None
    else:
        req_params = req_params_raw if isinstance(req_params_raw, dict) else {}

    placement = req_params.get("Placement", {})
    zone = placement.get("Zone", "")
    if zone:
        return "-".join(zone.split("-")[:-1])
    return None


def should_tag(rec: Dict[str, Any]) -> bool:
    """
    Decide if an event is a CVM/CDH resource creation we want to tag.
    """
    op = (rec.get("eventName") or rec.get("operationName") or rec.get("action") or "").lower()
    return ("create" in op) or (op in ("runinstances", "createinstance", "createcluster", "allocatehosts"))


def wait_for_cvm_running(instance_id: str, region: str, max_wait: int = 120, poll_interval: int = 10) -> str:
    """
    Poll CVM DescribeInstances until the instance reaches RUNNING state.
    
    Returns:
        "running" if instance reached RUNNING state
        "unauthorized" if DescribeInstances permission is missing
        "timeout" if timed out waiting
        "error" for unexpected terminal states
    """
    from index import make_tc_client
    from tencentcloud.cvm.v20170312 import cvm_client as tc_cvm_client, models as cvm_models

    elapsed = 0
    while elapsed < max_wait:
        try:
            client = make_tc_client("cvm", tc_cvm_client.CvmClient, region)
            if not client:
                print(json.dumps({"error": "cvm_state_check_no_client", "region": region}))
                return "error"

            req = cvm_models.DescribeInstancesRequest()
            req.InstanceIds = [instance_id]
            resp = client.DescribeInstances(req)

            instances = getattr(resp, "InstanceSet", [])
            if instances:
                state = getattr(instances[0], "InstanceState", "")
                if state == "RUNNING":
                    return "running"
                if state in ("STOPPED", "SHUTDOWN", "TERMINATING", "TERMINATED"):
                    print(json.dumps({
                        "warning": "cvm_unexpected_state",
                        "instance_id": instance_id,
                        "state": state
                    }))
                    return "error"
        except Exception as e:
            err_msg = str(e)
            if "UnauthorizedOperation" in err_msg or "not authorized" in err_msg:
                print(json.dumps({
                    "warning": "cvm_state_check_unauthorized",
                    "instance_id": instance_id,
                    "region": region,
                    "message": err_msg
                }))
                return "unauthorized"
            print(json.dumps({
                "error": "cvm_state_check_failed",
                "instance_id": instance_id,
                "elapsed_seconds": elapsed,
                "message": err_msg
            }))

        time.sleep(poll_interval)
        elapsed += poll_interval

    print(json.dumps({
        "warning": "cvm_state_check_timeout",
        "instance_id": instance_id,
        "max_wait": max_wait
    }))
    return "timeout"


def tag_cvm_attached_disks(instance_id: str, region: str, owner: str, owner_uin: str) -> int:
    """
    Find and tag all CBS disks attached to a CVM instance.
    
    Called after CVM tagging on RunInstances events. System disks created
    alongside CVM instances don't generate separate CreateCbsStorages events.
    """
    cvm_status = wait_for_cvm_running(instance_id, region)

    if cvm_status == "error":
        print(json.dumps({
            "warning": "cvm_disk_tagging_skipped",
            "instance_id": instance_id,
            "reason": "cvm_in_terminal_state"
        }))
        return 0

    if cvm_status == "unauthorized":
        print(json.dumps({
            "info": "cvm_disk_tagging_fallback",
            "instance_id": instance_id,
            "reason": "no_DescribeInstances_permission",
            "note": "Falling back to timed delay for disk query"
        }))
        return _query_and_tag_disks_with_retries(instance_id, region, owner, owner_uin)

    if cvm_status == "timeout":
        print(json.dumps({
            "warning": "cvm_disk_tagging_skipped",
            "instance_id": instance_id,
            "reason": "cvm_not_running_after_timeout"
        }))
        return 0

    return _query_and_tag_disks(instance_id, region, owner, owner_uin)


def _query_and_tag_disks_with_retries(instance_id: str, region: str, owner: str, owner_uin: str) -> int:
    """
    Fallback: query CBS disks with timed delays when CVM state polling is unavailable.
    """
    delays = [30, 30, 30]

    for attempt in range(len(delays) + 1):
        if attempt > 0:
            delay = delays[attempt - 1]
            print(json.dumps({
                "info": "cvm_disk_query_retrying",
                "instance_id": instance_id,
                "attempt": attempt + 1,
                "delay_seconds": delay,
                "reason": "no_disks_found_yet"
            }))
            time.sleep(delay)

        result = _query_and_tag_disks(instance_id, region, owner, owner_uin)
        if result > 0:
            return result

    print(json.dumps({
        "warning": "cvm_disk_query_no_disks_after_retries",
        "instance_id": instance_id,
        "attempts": len(delays) + 1,
        "total_wait_seconds": sum(delays)
    }))
    return 0


def _query_and_tag_disks(instance_id: str, region: str, owner: str, owner_uin: str) -> int:
    """
    Query CBS for disks attached to an instance and tag them.
    """
    from index import make_tc_client, tag_resource_qcs
    from tencentcloud.cbs.v20170312 import cbs_client, models as cbs_models
    from services.cbs import build_cbs_tags, parse_disk_usage

    try:
        client = make_tc_client("cbs", cbs_client.CbsClient, region)
        if not client:
            print(json.dumps({"error": "cvm_disk_tagging_no_cbs_client", "region": region}))
            return 0

        req = cbs_models.DescribeDisksRequest()
        req.Filters = [{"Name": "instance-id", "Values": [instance_id]}]
        resp = client.DescribeDisks(req)

        disks = getattr(resp, "DiskSet", [])

        if not disks:
            return 0

        print(json.dumps({
            "info": "cvm_disk_query_success",
            "instance_id": instance_id,
            "disks_found": len(disks)
        }))
    except Exception as e:
        print(json.dumps({
            "error": "cvm_disk_query_failed",
            "instance_id": instance_id,
            "region": region,
            "message": str(e)
        }))
        return 0

    disks_tagged = 0
    for disk in disks:
        disk_id = getattr(disk, "DiskId", "")
        disk_usage = getattr(disk, "DiskUsage", "SYSTEM_DISK")

        if not disk_id:
            continue

        tags = build_cbs_tags(
            owner=owner,
            disk_usage=parse_disk_usage(disk_usage),
            linked_cvm=True,
            cvm_project=""
        )

        qcs = f"qcs::cvm:{region}:uin/{owner_uin}:volume/{disk_id}"

        try:
            tag_resource_qcs(region, qcs, tags)
            disks_tagged += 1
            print(json.dumps({
                "success": "cvm_attached_disk_tagged",
                "instance_id": instance_id,
                "disk_id": disk_id,
                "disk_usage": parse_disk_usage(disk_usage),
                "qcs": qcs
            }))
        except Exception as e:
            print(json.dumps({
                "error": "cvm_attached_disk_tagging_failed",
                "instance_id": instance_id,
                "disk_id": disk_id,
                "qcs": qcs,
                "message": str(e)
            }))

    if disks_tagged > 0:
        print(json.dumps({
            "info": "cvm_attached_disks_tagged_summary",
            "instance_id": instance_id,
            "disks_tagged": disks_tagged,
            "total_disks": len(disks)
        }))

    return disks_tagged


def _resolve_cvm_region(rec: Dict[str, Any]) -> str:
    """
    Resolve the actual region for a CVM event.
    
    Priority:
    1. Placement.Zone from requestParameters (most reliable — chosen by user)
    2. resourceRegion from resourceSet
    3. extract_region() fallback
    """
    from index import extract_region
    zone_region = _extract_region_from_params(rec)
    if zone_region:
        return zone_region
    return extract_region(rec) or ""


def handle_cvm_tagging(rec: Dict[str, Any]) -> int:
    """
    Handle CVM/CDH tagging for RunInstances and AllocateHosts events.
    
    Returns:
        Number of resources tagged (1 for CVM/CDH + N attached disks + N attached ENIs)
    """
    from index import extract_account_uin, get_owner, tag_resource_qcs

    owner      = get_owner(rec)
    res_region = _resolve_cvm_region(rec)
    qcs        = extract_qcs(rec)
    tagged     = 0

    if not res_region or not qcs:
        print(json.dumps({
            "warning": "cvm_missing_region_or_qcs",
            "region": res_region,
            "qcs": qcs,
            "event": rec.get("eventName", "")
        }))
        return 0

    try:
        tag_resource_qcs(res_region, qcs, build_tags(owner))
        tagged += 1
    except Exception as te:
        print(json.dumps({"error": "cvm_tagging_failed", "qcs": qcs, "region": res_region, "message": str(te)}))
        return 0

    # Tag CBS disks and ENIs attached to this CVM
    if rec.get("eventName") == "RunInstances":
        instance_id = _extract_instance_id(rec)
        if instance_id:
            owner_uin = extract_account_uin(rec)
            try:
                disks_tagged = tag_cvm_attached_disks(instance_id, res_region, owner, owner_uin)
                tagged += disks_tagged
            except Exception as de:
                print(json.dumps({
                    "error": "cvm_disk_tagging_failed",
                    "instance_id": instance_id,
                    "region": res_region,
                    "message": str(de)
                }))
            try:
                enis_tagged = tag_cvm_attached_enis(instance_id, res_region, owner, owner_uin)
                tagged += enis_tagged
            except Exception as ee:
                print(json.dumps({
                    "error": "cvm_eni_tagging_failed",
                    "instance_id": instance_id,
                    "region": res_region,
                    "message": str(ee)
                }))

    return tagged


def tag_cvm_attached_enis(instance_id: str, region: str, owner: str, owner_uin: str) -> int:
    """
    Find and tag all ENIs attached to a CVM instance.
    
    Called after CVM tagging on RunInstances events. ENIs created alongside
    CVM instances don't generate separate CreateNetworkInterface events.
    """
    from index import make_tc_client, tag_resource_qcs
    from tencentcloud.vpc.v20170312 import vpc_client, models as vpc_models
    from services.eni import build_eni_tags

    try:
        client = make_tc_client("vpc", vpc_client.VpcClient, region)
        if not client:
            print(json.dumps({"error": "cvm_eni_tagging_no_vpc_client", "region": region}))
            return 0

        req = vpc_models.DescribeNetworkInterfacesRequest()
        req.Filters = [{"Name": "attachment.instance-id", "Values": [instance_id]}]
        resp = client.DescribeNetworkInterfaces(req)

        enis = getattr(resp, "NetworkInterfaceSet", [])
        if not enis:
            print(json.dumps({
                "info": "cvm_eni_query_none",
                "instance_id": instance_id,
                "region": region
            }))
            return 0

        print(json.dumps({
            "info": "cvm_eni_query_success",
            "instance_id": instance_id,
            "enis_found": len(enis)
        }))
    except Exception as e:
        print(json.dumps({
            "error": "cvm_eni_query_failed",
            "instance_id": instance_id,
            "region": region,
            "message": str(e)
        }))
        return 0

    enis_tagged = 0
    for eni in enis:
        eni_id = getattr(eni, "NetworkInterfaceId", "")
        if not eni_id:
            continue

        tags = build_eni_tags(owner=owner, linked_resource=instance_id)
        qcs = f"qcs::vpc:{region}:uin/{owner_uin}:eni/{eni_id}"

        try:
            tag_resource_qcs(region, qcs, tags)
            enis_tagged += 1
            print(json.dumps({
                "success": "cvm_attached_eni_tagged",
                "instance_id": instance_id,
                "eni_id": eni_id,
                "qcs": qcs
            }))
        except Exception as e:
            print(json.dumps({
                "error": "cvm_attached_eni_tagging_failed",
                "instance_id": instance_id,
                "eni_id": eni_id,
                "qcs": qcs,
                "message": str(e)
            }))

    if enis_tagged > 0:
        print(json.dumps({
            "info": "cvm_attached_enis_tagged_summary",
            "instance_id": instance_id,
            "enis_tagged": enis_tagged,
            "total_enis": len(enis)
        }))

    return enis_tagged


def _unwrap_resource_id(val):
    """Extract a plain string ID from a value that may be a list."""
    if isinstance(val, list):
        return val[0] if val and isinstance(val[0], str) else None
    if isinstance(val, str):
        if val.startswith("["):
            try:
                parsed = json.loads(val.replace("'", '"'))
                if isinstance(parsed, list) and parsed:
                    return parsed[0]
            except Exception:
                pass
        return val if val else None
    return None


def _extract_instance_id(rec: Dict[str, Any]) -> Optional[str]:
    """Extract CVM instance ID from resourceSet or responseElements."""
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list):
        for resource in resource_set:
            if isinstance(resource, dict) and "Instance" in resource.get("resourceTypeClass", ""):
                return _unwrap_resource_id(resource.get("resourceId"))

    resp_str = rec.get("responseElements", "")
    if resp_str and "InstanceIdSet" in resp_str:
        try:
            resp_data = json.loads(resp_str)
            ids = resp_data.get("InstanceIdSet", [])
            if ids:
                return ids[0]
        except Exception:
            pass

    return None
