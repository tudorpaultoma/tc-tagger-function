"""
Auto Scaling (AS) Tagging Service

Handles tagging for:
- Scaling groups (CreateAutoScalingGroup)
- Launch configurations (CreateLaunchConfiguration)

Both resources fire CloudAudit events under ResourceType "as".
A dedicated tagger-as-track monitors both event types.

CloudAudit quirks:
- resourceId may arrive as a stringified Python list: "['asg-xxx']"
- eventSource / eventRegion may point to a different region than where the
  resource actually lives
- Region discovery: tries detected region first, then COS_REGION fallback

QCS formats:
- Scaling group: qcs::as:{region}:uin/{uin}:auto-scaling-group/{asg_id}
- Launch config: qcs::as:{region}:uin/{uin}:launch-configuration/{asc_id}
"""

import json
import os
import datetime
from typing import List, Dict, Any, Optional


def build_asg_tags(owner: str) -> List[Dict[str, str]]:
    """
    Build tags for Auto Scaling Group resources.

    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCreated → TaggerOwner → TaggerProject → TaggerTTL

    Args:
        owner: Owner email/username

    Returns:
        List of tags to apply to scaling group
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",        "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",      "TagValue": today},
        {"TagKey": "TaggerCanDelete",    "TagValue": "YES"},
        {"TagKey": "TaggerTTL",          "TagValue": "3"},
        {"TagKey": "TaggerProject",      "TagValue": "n/a"},
    ]


def build_lc_tags(owner: str) -> List[Dict[str, str]]:
    """
    Build tags for Launch Configuration resources.

    Tags (displayed alphabetically in console):
    TaggerCanDelete → TaggerCreated → TaggerOwner → TaggerProject → TaggerTTL

    Args:
        owner: Owner email/username

    Returns:
        List of tags to apply to launch configuration
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",        "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",      "TagValue": today},
        {"TagKey": "TaggerCanDelete",    "TagValue": "YES"},
        {"TagKey": "TaggerTTL",          "TagValue": "3"},
        {"TagKey": "TaggerProject",      "TagValue": "n/a"},
    ]


def get_asg_info(asg_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query Auto Scaling Group details using DescribeAutoScalingGroups API.

    Returns:
        Dict with keys: AutoScalingGroupId, AutoScalingGroupName,
                        LaunchConfigurationId, VpcId, SubnetIdSet,
                        MaxSize, MinSize, DesiredCapacity
        None if not found or error
    """
    from index import make_tc_client
    from tencentcloud.autoscaling.v20180419 import autoscaling_client, models as as_models

    client = make_tc_client("as", autoscaling_client.AutoscalingClient, region)
    if not client:
        return None

    try:
        req = as_models.DescribeAutoScalingGroupsRequest()
        req.AutoScalingGroupIds = [asg_id]
        resp = client.DescribeAutoScalingGroups(req)

        groups = getattr(resp, "AutoScalingGroupSet", [])
        if not groups:
            return None

        grp = groups[0]
        return {
            "AutoScalingGroupId":   getattr(grp, "AutoScalingGroupId", ""),
            "AutoScalingGroupName": getattr(grp, "AutoScalingGroupName", ""),
            "LaunchConfigurationId": getattr(grp, "LaunchConfigurationId", ""),
            "VpcId":                getattr(grp, "VpcId", ""),
            "SubnetIdSet":          getattr(grp, "SubnetIdSet", []),
            "MaxSize":              getattr(grp, "MaxSize", 0),
            "MinSize":              getattr(grp, "MinSize", 0),
            "DesiredCapacity":      getattr(grp, "DesiredCapacity", 0),
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_asg_info_failed",
            "asg_id": asg_id,
            "region": region,
            "message": str(e)
        }))
        return None


def get_lc_info(lc_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query Launch Configuration details using DescribeLaunchConfigurations API.

    Returns:
        Dict with keys: LaunchConfigurationId, LaunchConfigurationName,
                        InstanceType, ImageId
        None if not found or error
    """
    from index import make_tc_client
    from tencentcloud.autoscaling.v20180419 import autoscaling_client, models as as_models

    client = make_tc_client("as", autoscaling_client.AutoscalingClient, region)
    if not client:
        return None

    try:
        req = as_models.DescribeLaunchConfigurationsRequest()
        req.LaunchConfigurationIds = [lc_id]
        resp = client.DescribeLaunchConfigurations(req)

        configs = getattr(resp, "LaunchConfigurationSet", [])
        if not configs:
            return None

        lc = configs[0]
        return {
            "LaunchConfigurationId":   getattr(lc, "LaunchConfigurationId", ""),
            "LaunchConfigurationName": getattr(lc, "LaunchConfigurationName", ""),
            "InstanceType":            getattr(lc, "InstanceType", ""),
            "ImageId":                 getattr(lc, "ImageId", ""),
        }
    except Exception as e:
        print(json.dumps({
            "error": "get_lc_info_failed",
            "lc_id": lc_id,
            "region": region,
            "message": str(e)
        }))
        return None


def _unwrap_id(val, prefix: str = ""):
    """Extract a plain string ID from a value that may be a list or string."""
    if isinstance(val, list):
        if prefix:
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


def _resolve_region(rec: Dict[str, Any], initial_region: str) -> list:
    """Build ordered candidate region list for resource lookup."""
    from index import _region_from_event_source
    cos_region = os.getenv("COS_REGION", "")
    event_source_region = _region_from_event_source(rec.get("eventSource", ""))
    request_region = rec.get("requestRegion") or rec.get("region")

    seen = set()
    candidates = []
    for r in [initial_region, event_source_region, request_region, cos_region]:
        if r and r not in seen:
            seen.add(r)
            candidates.append(r)
    return candidates


def handle_asg_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle Auto Scaling Group tagging for CreateAutoScalingGroup events.

    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs

    event_name = rec.get("eventName", "")
    if event_name != "CreateAutoScalingGroup":
        return False

    asg_id = None
    region = extract_region(rec)

    # Try resourceSet first
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list):
        for resource in resource_set:
            if not isinstance(resource, dict):
                continue
            raw_id = resource.get("resourceId", "")
            unwrapped = _unwrap_id(raw_id, "asg-")
            if unwrapped and unwrapped.startswith("asg-"):
                asg_id = unwrapped
                if not region:
                    region = resource.get("resourceRegion")
                break
        if not asg_id and resource_set:
            first = resource_set[0]
            if isinstance(first, dict):
                asg_id = _unwrap_id(first.get("resourceId"))
                if not region:
                    region = first.get("resourceRegion")

    # Fallback: parse responseElements for AutoScalingGroupId
    if not asg_id:
        resp_elems = _safe_dict(rec.get("responseElements"))
        asg_id = _unwrap_id(resp_elems.get("AutoScalingGroupId", ""))

    if not asg_id or not region:
        print(json.dumps({
            "warning": "asg_missing_id_or_region",
            "asg_id": asg_id,
            "region": region,
            "event": event_name
        }))
        return False

    # Query scaling group details — try candidate regions
    candidates = _resolve_region(rec, region)
    asg_info = None
    actual_region = region
    for candidate in candidates:
        asg_info = get_asg_info(asg_id, candidate)
        if asg_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "asg_found_in_alternate_region",
                    "asg_id": asg_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break
        if candidate != candidates[-1]:
            print(json.dumps({
                "info": "asg_region_retry",
                "asg_id": asg_id,
                "tried": candidate,
                "trying": candidates[candidates.index(candidate) + 1],
                "reason": "asg_not_found_in_region"
            }))

    region = actual_region

    asg_name = ""
    launch_config_id = ""
    vpc_id = ""
    if asg_info:
        asg_name = asg_info.get("AutoScalingGroupName", "") or ""
        launch_config_id = asg_info.get("LaunchConfigurationId", "") or ""
        vpc_id = asg_info.get("VpcId", "") or ""
        print(json.dumps({
            "info": "asg_details",
            "asg_id": asg_id,
            "asg_name": asg_name,
            "launch_config": launch_config_id,
            "vpc_id": vpc_id,
            "desired": asg_info.get("DesiredCapacity", 0),
            "min": asg_info.get("MinSize", 0),
            "max": asg_info.get("MaxSize", 0)
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
            asg_name = req_raw.get("AutoScalingGroupName", "") or ""
            launch_config_id = req_raw.get("LaunchConfigurationId", "") or ""
            vpc_id = req_raw.get("VpcId", "") or ""
        print(json.dumps({
            "warning": "asg_info_unavailable",
            "asg_id": asg_id,
            "note": "tagging with defaults from requestParameters"
        }))

    # Build QCS for Auto Scaling Group
    # qcs::as:{region}:uin/{uin}:auto-scaling-group/{asg_id}
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::as:{region}:uin/{account_uin}:auto-scaling-group/{asg_id}"

    owner = get_owner(rec)
    tags = build_asg_tags(owner=owner)

    print(json.dumps({
        "info": "asg_tagging",
        "asg_id": asg_id,
        "region": region,
        "qcs": qcs
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "asg_tagged",
            "asg_id": asg_id,
            "qcs": qcs,
            "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({
            "error": "asg_tagging_failed",
            "asg_id": asg_id,
            "qcs": qcs,
            "message": str(e)
        }))
        return False


def handle_lc_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle Launch Configuration tagging for CreateLaunchConfiguration events.

    Returns:
        True if tagging succeeded, False otherwise
    """
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs

    event_name = rec.get("eventName", "")
    if event_name != "CreateLaunchConfiguration":
        return False

    lc_id = None
    region = extract_region(rec)

    # Try resourceSet first
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list):
        for resource in resource_set:
            if not isinstance(resource, dict):
                continue
            raw_id = resource.get("resourceId", "")
            unwrapped = _unwrap_id(raw_id, "asc-")
            if unwrapped and unwrapped.startswith("asc-"):
                lc_id = unwrapped
                if not region:
                    region = resource.get("resourceRegion")
                break
        if not lc_id and resource_set:
            first = resource_set[0]
            if isinstance(first, dict):
                lc_id = _unwrap_id(first.get("resourceId"))
                if not region:
                    region = first.get("resourceRegion")

    # Fallback: parse responseElements for LaunchConfigurationId
    if not lc_id:
        resp_elems = _safe_dict(rec.get("responseElements"))
        lc_id = _unwrap_id(resp_elems.get("LaunchConfigurationId", ""))

    if not lc_id or not region:
        print(json.dumps({
            "warning": "lc_missing_id_or_region",
            "lc_id": lc_id,
            "region": region,
            "event": event_name
        }))
        return False

    # Query launch config details — try candidate regions
    candidates = _resolve_region(rec, region)
    lc_info = None
    actual_region = region
    for candidate in candidates:
        lc_info = get_lc_info(lc_id, candidate)
        if lc_info:
            actual_region = candidate
            if candidate != region:
                print(json.dumps({
                    "info": "lc_found_in_alternate_region",
                    "lc_id": lc_id,
                    "original_region": region,
                    "corrected_region": candidate
                }))
            break
        if candidate != candidates[-1]:
            print(json.dumps({
                "info": "lc_region_retry",
                "lc_id": lc_id,
                "tried": candidate,
                "trying": candidates[candidates.index(candidate) + 1],
                "reason": "lc_not_found_in_region"
            }))

    region = actual_region

    lc_name = ""
    instance_type = ""
    if lc_info:
        lc_name = lc_info.get("LaunchConfigurationName", "") or ""
        instance_type = lc_info.get("InstanceType", "") or ""
        print(json.dumps({
            "info": "lc_details",
            "lc_id": lc_id,
            "lc_name": lc_name,
            "instance_type": instance_type,
            "image_id": lc_info.get("ImageId", "")
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
            lc_name = req_raw.get("LaunchConfigurationName", "") or ""
            instance_type = req_raw.get("InstanceType", "") or ""
        print(json.dumps({
            "warning": "lc_info_unavailable",
            "lc_id": lc_id,
            "note": "tagging with defaults from requestParameters"
        }))

    # Build QCS for Launch Configuration
    # qcs::as:{region}:uin/{uin}:launch-configuration/{lc_id}
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::as:{region}:uin/{account_uin}:launch-configuration/{lc_id}"

    owner = get_owner(rec)
    tags = build_lc_tags(owner=owner)

    print(json.dumps({
        "info": "lc_tagging",
        "lc_id": lc_id,
        "region": region,
        "qcs": qcs
    }))

    try:
        tag_resource_qcs(region, qcs, tags)
        print(json.dumps({
            "success": "lc_tagged",
            "lc_id": lc_id,
            "qcs": qcs,
            "tags_applied": len(tags)
        }))
        return True
    except Exception as e:
        print(json.dumps({
            "error": "lc_tagging_failed",
            "lc_id": lc_id,
            "qcs": qcs,
            "message": str(e)
        }))
        return False
