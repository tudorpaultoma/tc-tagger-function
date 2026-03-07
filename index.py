#!/usr/bin/env python3
"""
SCF Resource Tagger

Automatically tags newly created CVM instances, CDH (Dedicated Hosts), CLB (Cloud Load Balancer),
and CBS disks based on CloudAudit events. This SCF function processes CloudAudit logs delivered 
to COS and applies standardized tags to resources for better management and cost tracking.

CloudAudit tracks are global and automatically monitor all regions.

Author: Tudor Toma
Version: 1.9.2
License: Apache 2.0
"""

import os
import sys
import json
import datetime
import traceback
import re
import gzip
import time
from typing import List, Dict, Any, Optional

# Ensure vendored libraries are available for SCF runtime
THIS_DIR = os.path.dirname(__file__)
for path in (
    os.path.join(THIS_DIR, "package"),  # Local development dependencies
    "/var/user/package",                # SCF extracts ZIP here
    "/opt/python"                       # Layer path if ever used
):
    try:
        if path and path not in sys.path:
            sys.path.append(path)
    except Exception:
        pass

# Tencent Cloud SDKs
from qcloud_cos import CosConfig, CosS3Client
from tencentcloud.common import credential
from tencentcloud.tag.v20180813 import tag_client, models as tag_models
from tencentcloud.cloudaudit.v20190319 import cloudaudit_client, models as audit_models
from tencentcloud.cbs.v20170312 import cbs_client, models as cbs_models
from tencentcloud.cvm.v20170312 import cvm_client as tc_cvm_client, models as cvm_models

# Configuration from environment variables
COS_BUCKET       = os.getenv("COS_BUCKET")
COS_REGION       = os.getenv("COS_REGION")
COS_PREFIX       = (os.getenv("COS_PREFIX") or "").strip().rstrip("/")
COS_BASE_PREFIX  = (os.getenv("COS_BASE_PREFIX") or "cloudaudit").strip().rstrip("/")


def _build_cred():
    """
    Build TencentCloud credentials from env or CVM role metadata.
    """
    sid = os.getenv("TENCENTCLOUD_SECRETID") or os.getenv("TENCENTCLOUD_SECRET_ID")
    sk  = os.getenv("TENCENTCLOUD_SECRETKEY") or os.getenv("TENCENTCLOUD_SECRET_KEY")
    tok = os.getenv("TENCENTCLOUD_SESSIONTOKEN") or os.getenv("TENCENTCLOUD_SESSION_TOKEN")
    if sid and sk:
        return credential.Credential(sid, sk, tok or "")
    try:
        cred = credential.CVMRoleCredential()
        if getattr(cred, "get_secret_id")() and getattr(cred, "get_secret_key")():
            return cred
    except Exception:
        pass
    return None


def make_tc_client(service_key: str, client_cls, region: str):
    """
    Create a TencentCloud API client with proper HTTP profile.
    """
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile

    http_profile = HttpProfile()
    http_profile.endpoint = f"{service_key}.tencentcloudapi.com"
    client_profile = ClientProfile(httpProfile=http_profile)

    cred = _build_cred()
    if not cred:
        print(json.dumps({"error": "tc_sdk_creds_missing", "service": service_key, "region": region}))
        return None
    return client_cls(cred, region, client_profile)


def make_cos_client(region: str) -> CosS3Client:
    """
    Create a COS client for accessing object storage.
    """
    sid = os.getenv("TENCENTCLOUD_SECRETID") or os.getenv("TENCENTCLOUD_SECRET_ID")
    sk  = os.getenv("TENCENTCLOUD_SECRETKEY") or os.getenv("TENCENTCLOUD_SECRET_KEY")
    tok = os.getenv("TENCENTCLOUD_SESSIONTOKEN") or os.getenv("TENCENTCLOUD_SESSION_TOKEN")
    if not sid or not sk:
        raise RuntimeError("COS credentials not found in environment.")
    cfg = CosConfig(Region=region, SecretId=sid, SecretKey=sk, Token=tok or "")
    return CosS3Client(cfg)


def ensure_cos_bucket(bucket: str, region: str) -> bool:
    """
    Ensure the COS bucket exists, creating it if necessary.
    """
    client = make_cos_client(region)
    try:
        client.head_bucket(Bucket=bucket)
        print(json.dumps({"step": "cos_bucket", "status": "exists", "bucket": bucket, "region": region}))
        return True
    except Exception:
        try:
            client.create_bucket(Bucket=bucket)
            print(json.dumps({"step": "cos_bucket", "status": "created", "bucket": bucket, "region": region}))
            return True
        except Exception as e:
            print(json.dumps({"step": "cos_bucket", "error": str(e), "bucket": bucket, "region": region}))
            return False


def ensure_audit_track_to_cos(bucket_region: str, bucket: str) -> Optional[str]:
    """
    Ensure CloudAudit tracks exist for monitoring resource creation events.
    Creates separate tracks per service type (CVM, CLB, CBS) delivering to COS.
    
    Idempotent: Only creates/updates tracks if they don't exist or are misconfigured.
    This prevents cascade loops where track deletion causes event re-delivery.
    
    Architecture:
        - Track 1 (tagger-cvm-track): ResourceType="cvm" → RunInstances, AllocateHosts
        - Track 2 (tagger-clb-track): ResourceType="clb" → CreateLoadBalancer
        - Track 3 (tagger-cbs-track): ResourceType="cbs" → ["*"] (all CBS write events)
        - All tracks deliver to same COS bucket → single SCF function processes all events
    
    Important:
        CloudAudit API does NOT support ResourceType="*" (wildcard) with specific EventNames.
        Each service type requires a dedicated track with specific ResourceType.
        CBS track uses EventNames=["*"] because CloudAudit may not recognize specific
        event names like "CreateDisks" for the CBS service, causing silent creation failure.
        Each track creation is individually error-handled so one failure doesn't block others.
    
    Note: 
        CloudAudit tracks are global and automatically monitor all regions.
    
    Args:
        bucket_region: The region where the COS bucket is located
        bucket: The COS bucket name
    
    Returns:
        TrackId of first created track (for backward compatibility), None on failure
    """
    if not bucket_region or not bucket:
        print(json.dumps({"step": "audit_track", "error": "missing bucket_region or bucket"}))
        return None

    # CloudAudit API region - using European endpoint
    # CloudAudit tracks automatically monitor ALL regions globally
    CLOUDAUDIT_API_REGION = "eu-frankfurt"
    client = make_tc_client("cloudaudit", cloudaudit_client.CloudauditClient, CLOUDAUDIT_API_REGION)
    if client is None:
        print(json.dumps({"step": "audit_track", "error": "no SDK credentials available", "api_region": CLOUDAUDIT_API_REGION}))
        return None

    # Track 1: CVM/CDH
    cvm_track_name = "tagger-cvm-track"
    cvm_event_names = ["RunInstances", "AllocateHosts"]
    
    # Track 2: CLB
    clb_track_name = "tagger-clb-track"
    clb_event_names = ["CreateLoadBalancer"]
    
    # Track 3: CBS — use ["*"] to capture ALL CBS write events
    # CloudAudit may not recognize specific event names like "CreateDisks" for
    # ResourceType="cbs", causing track creation to fail silently.
    # Using wildcard ensures we capture CreateDisks/CreateCbsStorages/AttachDisks etc.
    cbs_track_name = "tagger-cbs-track"
    cbs_event_names = ["*"]
    
    # Prepare storage config
    storage = audit_models.Storage()
    setattr(storage, "StorageType", "cos")
    setattr(storage, "StorageRegion", bucket_region)
    short_bucket = bucket.split("-")[0]
    setattr(storage, "StorageName", short_bucket)
    prefix_base = re.sub(r"[^A-Za-z0-9]", "", COS_BASE_PREFIX or "cloudaudit")
    if len(prefix_base) < 3:
        prefix_base = (prefix_base + "logs")[:3]
    setattr(storage, "StoragePrefix", prefix_base[:40])

    try:
        dreq = audit_models.DescribeAuditTracksRequest()
        setattr(dreq, "PageNumber", 1)
        setattr(dreq, "PageSize", 50)
        dresp = client.DescribeAuditTracks(dreq)
        existing_tracks = getattr(dresp, "Tracks", []) or []
        print(json.dumps({"step": "audit_tracks", "existing_count": len(existing_tracks),
                          "names": [getattr(t, "Name", "") for t in existing_tracks]}))
    except Exception as e:
        print(json.dumps({"step": "audit_tracks", "error": "list_failed", "message": str(e)}))
        return None

    def _find_track(tracks, name):
        for tr in tracks:
            if getattr(tr, "Name", "") == name:
                return tr
        return None

    def _ensure_track(track_name, resource_type, event_names):
        """Create or validate a single track. Returns track_id or None."""
        tr = _find_track(existing_tracks, track_name)
        track_id = getattr(tr, "TrackId", None) if tr else None
        track_valid = False

        if tr:
            track_status = getattr(tr, "Status", 0)
            track_rt = getattr(tr, "ResourceType", "")
            track_evts = sorted(getattr(tr, "EventNames", []) or [])
            if track_status == 1 and track_rt == resource_type and track_evts == sorted(event_names):
                print(json.dumps({"step": track_name, "status": "exists", "track_id": track_id,
                                  "action": "skip_recreation", "event_names": track_evts}))
                track_valid = True
            else:
                print(json.dumps({"step": track_name, "status": "needs_update", "track_id": track_id,
                                  "current_status": track_status, "current_rt": track_rt,
                                  "current_events": track_evts, "desired_events": sorted(event_names)}))

        if not track_valid:
            if track_id:
                try:
                    delreq = audit_models.DeleteAuditTrackRequest()
                    setattr(delreq, "TrackId", track_id)
                    client.DeleteAuditTrack(delreq)
                    print(json.dumps({"step": track_name, "status": "deleted", "track_id": track_id}))
                except Exception as del_err:
                    print(json.dumps({"warning": f"delete_{track_name}_failed", "error": str(del_err)}))

            try:
                print(json.dumps({"debug": f"create_{track_name}", "resource_type": resource_type, "event_names": event_names}))
                creq = audit_models.CreateAuditTrackRequest()
                setattr(creq, "Name", track_name)
                setattr(creq, "Status", 1)
                setattr(creq, "ActionType", "Write")
                setattr(creq, "ResourceType", resource_type)
                setattr(creq, "EventNames", event_names)
                setattr(creq, "Storage", storage)
                cresp = client.CreateAuditTrack(creq)
                track_id = getattr(cresp, "TrackId", None)
                print(json.dumps({"step": track_name, "status": "created", "track_id": track_id,
                                  "resource_type": resource_type, "events": event_names}))
            except Exception as create_err:
                print(json.dumps({"error": f"create_{track_name}_failed",
                                  "resource_type": resource_type, "event_names": event_names,
                                  "message": str(create_err), "traceback": traceback.format_exc()}))
                track_id = None

        return track_id

    cvm_track_id = _ensure_track(cvm_track_name, "cvm", cvm_event_names)
    clb_track_id = _ensure_track(clb_track_name, "clb", clb_event_names)
    cbs_track_id = _ensure_track(cbs_track_name, "cbs", cbs_event_names)

    return cvm_track_id


def ensure_audit_tracks_all_regions(bucket_region: str, bucket: str) -> Dict[str, Optional[str]]:
    """
    Ensure a global CloudAudit track exists.
    CloudAudit tracks automatically monitor all regions by default.
    
    Returns:
        Dictionary with single track_id
    """
    print(json.dumps({
        "step": "audit_setup",
        "cos_bucket_region": bucket_region,
        "note": "CloudAudit tracks monitor all regions globally"
    }))
    
    # Create or update the single global track
    track_id = ensure_audit_track_to_cos(bucket_region, bucket)
    
    # Return dict with track info
    return {"global": track_id}


def build_tags(owner: str) -> List[Dict[str, str]]:
    """
    Build standardized tags for resources (CVM, CDH).
    
    Logical Tag Order (for code organization):
    1. TaggerOwner
    2. TaggerCreated
    3. TaggerAutoOff
    4. TaggerAutoStart
    5. TaggerCanDelete
    6. TaggerTTL
    7. TaggerProject
    
    Note: Tags will be displayed ALPHABETICALLY in the Tencent Cloud console:
    TaggerAutoOff → TaggerAutoStart → TaggerCanDelete → TaggerCreated → 
    TaggerOwner → TaggerProject → TaggerTTL
    
    This is controlled by the Tag service, not by the order we send them.
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",     "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",   "TagValue": today},
        {"TagKey": "TaggerAutoOff",   "TagValue": "YES"},
        {"TagKey": "TaggerAutoStart", "TagValue": "NO"},
        {"TagKey": "TaggerCanDelete", "TagValue": "YES"},
        {"TagKey": "TaggerTTL",       "TagValue": "7"},
        {"TagKey": "TaggerProject",   "TagValue": "n/a"},
    ]


def build_clb_tags(owner: str) -> List[Dict[str, str]]:
    """
    Build tags for CLB (Cloud Load Balancer) resources.
    
    CLBs can only be created or deleted (no start/stop operations).
    
    Logical Tag Order (for code organization):
    1. TaggerOwner
    2. TaggerCreated
    3. TaggerCanDelete
    4. TaggerTTL
    5. TaggerProject
    
    Note: Tags will be displayed ALPHABETICALLY in the Tencent Cloud console:
    TaggerCanDelete → TaggerCreated → TaggerOwner → TaggerProject → TaggerTTL
    
    This is controlled by the Tag service, not by the order we send them.
    
    Args:
        owner: Owner email/username
    
    Returns:
        List of tags to apply to CLB
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",     "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",   "TagValue": today},
        {"TagKey": "TaggerCanDelete", "TagValue": "YES"},
        {"TagKey": "TaggerTTL",       "TagValue": "7"},
        {"TagKey": "TaggerProject",   "TagValue": "n/a"},
    ]


def build_cbs_tags(owner: str, disk_usage: str = "SYSTEM", linked_cvm: bool = False, cvm_project: str = "") -> List[Dict[str, str]]:
    """
    Build tags for CBS disks.
    
    Logical Tag Order (for code organization):
    1. TaggerOwner
    2. TaggerCreated
    3. TaggerUsage (default: SYSTEM)
    4. TaggerLinkedCVM
    5. TaggerCanDelete
    6. TaggerTTL
    7. TaggerProject
    
    Note: Tags will be displayed ALPHABETICALLY in the Tencent Cloud console:
    TaggerCanDelete → TaggerCreated → TaggerLinkedCVM → TaggerOwner → 
    TaggerProject → TaggerTTL → TaggerUsage
    
    This is controlled by the Tag service, not by the order we send them.
    
    Args:
        owner: Owner email/username
        disk_usage: SYSTEM or DATA (from CBS DiskUsage field, default: SYSTEM)
        linked_cvm: True if disk is attached to a CVM (default: False)
        cvm_project: Project name from CVM (may be empty)
    
    Returns:
        List of tags to apply to CBS disk
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


def find_recent_disk_with_retry(region: str, event_time: int, window_seconds: int = 300, 
                                  max_retries: int = 4, delays: Optional[List[int]] = None) -> Optional[str]:
    """
    Find most recently created disk with retry logic for timing issues.
    
    CBS disk provisioning can take 10-30s for pay-as-you-go, but **minutes** for
    monthly subscription (prepaid) disks where the order must complete first.
    This function retries with increasing delays to handle both cases.
    
    Args:
        region: Region to search
        event_time: CloudAudit event timestamp (unix epoch)
        window_seconds: Time window in seconds (default 300 = 5 minutes)
        max_retries: Maximum number of retry attempts (default 4)
        delays: List of delay seconds between retries (default [10, 20, 30, 40] = 100s total)
    
    Returns:
        Disk ID of most recent disk, or None if not found after all retries
    """
    if delays is None:
        delays = [10, 20, 30, 40]  # Total 100s wait — enough for prepaid provisioning
    
    for attempt in range(max_retries + 1):  # +1 for initial attempt
        disk_id = find_recent_disk(region, event_time, window_seconds)
        
        if disk_id:
            if attempt > 0:
                print(json.dumps({
                    "info": "cbs_retry_succeeded",
                    "attempt": attempt + 1,
                    "disk_id": disk_id
                }))
            return disk_id
        
        # If not found and we have retries left, wait and try again
        if attempt < max_retries:
            delay = delays[attempt] if attempt < len(delays) else delays[-1]
            print(json.dumps({
                "info": "cbs_disk_not_found_retrying",
                "attempt": attempt + 1,
                "delay_seconds": delay,
                "reason": "disk_provisioning_may_be_in_progress"
            }))
            time.sleep(delay)
    
    # All retries exhausted
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
    Used when CloudAudit event has empty resourceSet (console/prepaid disks).
    
    Sorts results by creation time descending and skips disks that already
    have Tagger tags applied.
    
    Args:
        region: Region to search
        event_time: CloudAudit event timestamp (unix epoch)
        window_seconds: Time window in seconds (default 300 = 5 minutes)
    
    Returns:
        Disk ID of most recent untagged disk, or None
    """
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
                    
                    # Skip disks that already have Tagger tags
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


def get_disk_info(disk_id: str, region: str) -> Optional[Dict[str, Any]]:
    """
    Query CBS disk details using DescribeDisks API.
    
    Returns:
        Dict with keys: DiskState, InstanceId, DiskUsage, CreateTime
        None if disk not found or error
    """
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
    
    Returns:
        Dict of {TagKey: TagValue}, empty dict if error
    """
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


def parse_disk_usage(disk_usage: str) -> str:
    """
    Convert CBS DiskUsage API field to tag value.
    
    Args:
        disk_usage: SYSTEM_DISK or DATA_DISK
    
    Returns:
        SYSTEM or DATA
    """
    if "SYSTEM" in disk_usage.upper():
        return "SYSTEM"
    return "DATA"


def handle_cbs_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle CBS disk tagging for CreateCbsStorages, CreateDisks, and AttachDisks events.
    
    Strategy:
    - For attached disks: Copy TaggerProject from CVM, recreate other tags
    - For unattached disks: Apply default tags with TaggerProject=""
    - Add TaggerUsage (SYSTEM/DATA) and TaggerLinkedCVM (YES/NO) tags
    
    Returns:
        True if tagging succeeded, False otherwise
    """
    event_name = rec.get("eventName", "")
    if event_name not in ("CreateCbsStorages", "CreateDisks", "AttachDisks"):
        return False
    
    # Extract disk ID and region
    disk_id = None
    region = extract_region(rec)
    
    # Try resourceSet first (console events)
    resource_set = rec.get("resourceSet", [])
    
    if resource_set and isinstance(resource_set, list) and len(resource_set) > 0:
        first_resource = resource_set[0]
        if isinstance(first_resource, dict):
            disk_id_raw = first_resource.get("resourceId")
            # Handle resourceId formats: "disk-xxx", "['disk-xxx']", '["disk-xxx"]'
            if disk_id_raw and isinstance(disk_id_raw, str):
                # Check for Python/JSON list-like string: "['disk-xxx']" or '["disk-xxx"]'
                if disk_id_raw.startswith("[") and disk_id_raw.endswith("]"):
                    try:
                        parsed = json.loads(disk_id_raw.replace("'", '"'))
                        if isinstance(parsed, list) and parsed:
                            disk_id = parsed[0]
                        else:
                            disk_id = disk_id_raw
                    except Exception:
                        # Regex fallback: extract disk-xxxxx from any format
                        m = re.search(r"(disk-[a-zA-Z0-9]+)", disk_id_raw)
                        disk_id = m.group(1) if m else disk_id_raw
                else:
                    disk_id = disk_id_raw
            else:
                disk_id = disk_id_raw
            if not region:
                region = first_resource.get("resourceRegion")
    
    # Try to extract disk ID from responseElements (fallback)
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
    
    # For AttachDisks, also try requestParameters
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
    
    # For CreateCbsStorages/CreateDisks, check requestParameters for disk count (API-created disks)
    if not disk_id and event_name in ("CreateCbsStorages", "CreateDisks"):
        # Console-created disks: resourceSet empty initially, query CBS for recent disks
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
    
    # Get disk info (no age check - tag immediately)
    disk_info = get_disk_info(disk_id, region)
    if not disk_info:
        print(json.dumps({"error": "cbs_tagging_skipped", "reason": "disk_info_not_available", "disk_id": disk_id}))
        return False
    
    # Get disk state and attached instance
    disk_state = disk_info.get("DiskState", "")
    instance_id = disk_info.get("InstanceId", "")
    disk_usage = disk_info.get("DiskUsage", "DATA_DISK")
    
    # Get owner from audit record
    owner = get_owner(rec)
    
    # Determine tagging strategy
    if disk_state == "ATTACHED" and instance_id:
        # Strategy 1: Copy project from CVM
        cvm_tags = get_cvm_tags(instance_id, region)
        
        # Extract TaggerProject from CVM (may be empty)
        cvm_project = cvm_tags.get("TaggerProject", "")
        
        # Build CBS tags
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
        # Strategy 2: Unattached disk - default tags
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
    
    # Get UIN for QCS
    user_id = rec.get("userIdentity", {})
    if isinstance(user_id, dict):
        owner_uin = user_id.get("accountId") or user_id.get("principalId") or user_id.get("ownerUin") or ""
    else:
        owner_uin = ""
    
    # Build QCS for CBS disk - CBS uses cvm service type with volume prefix
    # Format: qcs::cvm:region:uin/xxx:volume/disk-xxx
    qcs = f"qcs::cvm:{region}:uin/{owner_uin}:volume/{disk_id}"
    
    # Apply tags using Tag API (standard approach for all Tencent Cloud resources)
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


def wait_for_cvm_running(instance_id: str, region: str, max_wait: int = 120, poll_interval: int = 10) -> str:
    """
    Poll CVM DescribeInstances until the instance reaches RUNNING state.
    
    Args:
        instance_id: CVM instance ID
        region: Resource region
        max_wait: Maximum seconds to wait (default 120s)
        poll_interval: Seconds between polls (default 10s)
    
    Returns:
        "running" if instance reached RUNNING state
        "unauthorized" if DescribeInstances permission is missing
        "timeout" if timed out waiting
        "error" for unexpected terminal states
    """
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
            # Detect permission error immediately — no point retrying
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
    
    Called after CVM tagging on RunInstances events. When a CVM is created,
    its system disk (and any data disks) are provisioned automatically but
    no separate CreateCbsStorages CloudAudit event is fired. This function
    waits for the CVM to reach RUNNING state (disks are attached by then),
    then queries CBS for disks and tags them.
    
    If DescribeInstances permission is unavailable, falls back to a timed
    delay with retries for the disk query.
    
    Args:
        instance_id: CVM instance ID (e.g. ins-pzkqhljc)
        region: Resource region (e.g. eu-frankfurt)
        owner: Owner email/username for TaggerOwner tag
        owner_uin: Account UIN for QCS string
    
    Returns:
        Number of disks successfully tagged
    """
    # Try CVM state polling first
    cvm_status = wait_for_cvm_running(instance_id, region)
    
    if cvm_status == "error":
        print(json.dumps({
            "warning": "cvm_disk_tagging_skipped",
            "instance_id": instance_id,
            "reason": "cvm_in_terminal_state"
        }))
        return 0
    
    if cvm_status == "unauthorized":
        # Fallback: no DescribeInstances permission, use timed delay + retries
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
    
    # CVM is RUNNING — query disks directly (should be available immediately)
    return _query_and_tag_disks(instance_id, region, owner, owner_uin)


def _query_and_tag_disks_with_retries(instance_id: str, region: str, owner: str, owner_uin: str) -> int:
    """
    Fallback: query CBS disks with timed delays when CVM state polling is unavailable.
    Uses delays [30, 30, 30] (90s total) to allow time for CVM provisioning.
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
        
        # Check if the query itself failed (not just empty) — logged inside _query_and_tag_disks
    
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
    Returns number of disks tagged (0 if none found or error).
    """
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
    
    # Tag each disk found
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
            cvm_project=""  # CVM just created, TaggerProject is "n/a"
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


def handle_clb_tagging(rec: Dict[str, Any]) -> bool:
    """
    Handle CLB (Cloud Load Balancer) tagging for CreateLoadBalancer events.
    
    Applies CLB-specific tags (CLBs can only be created/deleted, no start/stop):
    - TaggerOwner: Resource creator (email, username, or account ID)
    - TaggerCreated: Creation date
    - TaggerTTL: Time-to-live in days (7)
    - TaggerDelete: Auto-deletion flag (YES)
    - TaggerProject: Project designation (n/a)
    
    Returns:
        True if tagging succeeded, False otherwise
    """
    event_name = rec.get("eventName", "")
    if event_name != "CreateLoadBalancer":
        return False
    
    # Extract LB ID and region
    lb_id = None
    region = None
    
    # Try resourceSet first (console events)
    resource_set = rec.get("resourceSet", [])
    if resource_set and isinstance(resource_set, list) and len(resource_set) > 0:
        first_resource = resource_set[0]
        if isinstance(first_resource, dict):
            lb_id_raw = first_resource.get("resourceId")
            # Handle case where resourceId is a string like "['lb-xxx']"
            if lb_id_raw and isinstance(lb_id_raw, str):
                # Try to parse as JSON array
                if lb_id_raw.startswith("[") and lb_id_raw.endswith("]"):
                    try:
                        # Replace single quotes with double quotes for valid JSON
                        lb_id_json = lb_id_raw.replace("'", '"')
                        lb_ids_list = json.loads(lb_id_json)
                        if lb_ids_list and isinstance(lb_ids_list, list):
                            lb_id = lb_ids_list[0]
                        else:
                            lb_id = lb_id_raw
                    except Exception:
                        # If parsing fails, use as-is
                        lb_id = lb_id_raw
                else:
                    lb_id = lb_id_raw
            else:
                lb_id = lb_id_raw
            # Always prefer resourceRegion from resourceSet
            region = first_resource.get("resourceRegion")
    
    # Fallback to extract_region if resourceSet didn't provide region
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
    
    # Build QCS resource identifier
    account_uin = extract_account_uin(rec)
    qcs = f"qcs::clb:{region}:uin/{account_uin}:clb/{lb_id}"
    
    print(json.dumps({
        "info": "clb_tagging",
        "lb_id": lb_id,
        "region": region,
        "qcs": qcs
    }))
    
    # Build and apply tags
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


def get_owner(rec: Dict[str, Any]) -> str:
    """
    Extract owner information from a CloudAudit record.
    """
    ui = rec.get("userIdentity") or rec.get("user", {}) or {}
    if not isinstance(ui, dict):
        return "unknown"
    email = ui.get("userEmail") or ui.get("email")
    if email:
        return email
    user_name = ui.get("userName") or ui.get("name") or ui.get("displayName")
    if user_name:
        return user_name
    account_id = ui.get("accountId")
    if account_id:
        return f"account:{account_id}"
    uin = ui.get("uin") or ui.get("principalId") or ui.get("ownerUin")
    return f"uin:{uin}" if uin else "unknown"


def make_tag_client(region: str):
    """
    Create a Tag API client for the specified region.
    """
    cred = _build_cred()
    return tag_client.TagClient(cred, region)


def tag_resource_qcs(region: str, qcs: str, tags: List[Dict[str, str]]) -> None:
    """
    Apply tags to a resource using its QCS identifier.
    """
    client = make_tag_client(region)
    req = tag_models.TagResourcesRequest()
    req.ResourceList = [qcs]
    req.Tags = tags
    
    try:
        resp = client.TagResources(req)
    except Exception as e:
        print(json.dumps({
            "error": "tag_api_call_failed",
            "qcs": qcs,
            "region": region,
            "message": str(e)
        }))
        raise


def extract_region(rec: Dict[str, Any]) -> Optional[str]:
    """
    Extract region information from a CloudAudit record.
    
    Priority:
    1. resourceRegion from resourceSet (most accurate for the actual resource)
    2. Top-level region / requestRegion fields
    3. eventRegion (CloudAudit processing region — often differs from resource region)
    """
    # Best source: resourceRegion from resourceSet
    resource_set = rec.get("resourceSet", [])
    if isinstance(resource_set, list):
        for resource in resource_set:
            if isinstance(resource, dict):
                rr = resource.get("resourceRegion")
                if rr:
                    return rr

    return rec.get("region") or rec.get("requestRegion") or rec.get("eventRegion")


def extract_account_uin(rec: Dict[str, Any]) -> str:
    """
    Extract account UIN from a CloudAudit record.
    Returns the account ID, principal ID, or owner UIN.
    """
    user_id = rec.get("userIdentity", {})
    if isinstance(user_id, dict):
        return user_id.get("accountId") or user_id.get("principalId") or user_id.get("ownerUin") or ""
    return ""


def extract_qcs(rec: Dict[str, Any]) -> Optional[str]:
    """
    Build QCS identifier for CVM instance or CDH creation events.
    Supports:
    - CVM RunInstances events -> qcs::cvm:{region}:uin/{uin}:instance/{instance_id}
    - CDH AllocateHosts events -> qcs::cvm:{region}:uin/{uin}:host/{host_id}
    """
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
            # Find the actual CVM instance in resourceSet
            for resource in resource_set:
                if not isinstance(resource, dict):
                    continue
                resource_type_class = resource.get("resourceTypeClass", "")
                # Look for QCS::CVM::Instance (the actual instance, not keypair/sg/etc)
                if "Instance" in resource_type_class and "Keypair" not in resource_type_class:
                    instance_id     = resource.get("resourceId")
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
                    resource_region = None  # Initialize
                    # Determine region from requestParameters if missing
                    if not resource_region:
                        req_params_raw = rec.get("requestParameters", {})
                        # Parse if it's a JSON string
                        if isinstance(req_params_raw, str):
                            try:
                                req_params = json.loads(req_params_raw)
                            except Exception:
                                req_params = {}
                        else:
                            req_params = req_params_raw if isinstance(req_params_raw, dict) else {}
                        
                        placement = req_params.get("Placement", {})
                        zone = placement.get("Zone", "")
                        if zone:
                            resource_region = "-".join(zone.split("-")[:-1])
                    # owner uin
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
        # Try resourceSet first - filter for actual host
        resource_set = rec.get("resourceSet", [])
        if resource_set:
            # Find the actual CDH host in resourceSet
            for resource in resource_set:
                if not isinstance(resource, dict):
                    continue
                resource_type_class = resource.get("resourceTypeClass", "")
                # Look for QCS::CVM::Host or similar (the actual host)
                if "Host" in resource_type_class:
                    host_id         = resource.get("resourceId")
                    resource_region = resource.get("resourceRegion")
                    user_id         = rec.get("userIdentity", {})
                    if isinstance(user_id, dict):
                        owner_uin = user_id.get("accountId") or user_id.get("principalId") or user_id.get("ownerUin") or ""
                    else:
                        owner_uin = ""
                    if host_id and resource_region:
                        return f"qcs::cvm:{resource_region}:uin/{owner_uin}:host/{host_id}"
                    break

        # Fallback: parse responseElements
        resp_str = rec.get("responseElements", "")
        if resp_str and "HostIdSet" in resp_str:
            try:
                resp = json.loads(resp_str)
                ids  = resp.get("HostIdSet", [])
                if ids:
                    host_id = ids[0]
                    resource_region = None
                    # Determine region from requestParameters if missing
                    if not resource_region:
                        req_params_raw = rec.get("requestParameters", {})
                        # Parse if it's a JSON string
                        if isinstance(req_params_raw, str):
                            try:
                                req_params = json.loads(req_params_raw)
                            except Exception:
                                req_params = {}
                        else:
                            req_params = req_params_raw if isinstance(req_params_raw, dict) else {}
                        
                        placement = req_params.get("Placement", {})
                        zone = placement.get("Zone", "")
                        if zone:
                            resource_region = "-".join(zone.split("-")[:-1])
                    
                    # Extract region from event if still missing
                    if not resource_region:
                        resource_region = extract_region(rec)
                    
                    # owner uin
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

    # Generic fallback (not used for CVM-only tagging)
    service = rec.get("service") or rec.get("eventSource")
    params_raw = rec.get("requestParameters", {})
    # Parse requestParameters if it's a JSON string
    if isinstance(params_raw, str):
        try:
            params = json.loads(params_raw)
        except Exception:
            params = {}
    else:
        params = params_raw if isinstance(params_raw, dict) else {}
    
    rid     = rec.get("resourceId") or params.get("ResourceId")
    region  = extract_region(rec)
    if service and rid and region:
        ui = rec.get("userIdentity", {})
        if isinstance(ui, dict):
            owner = ui.get("accountId") or ui.get("principalId") or ui.get("ownerUin") or ""
        else:
            owner = ""
        upart = f"uin/{owner}" if owner else ""
        return f"qcs::{service}:{region}:{upart}:resourceId/{rid}"

    return None


def read_cos_object(bucket: str, key: str, region: str) -> str:
    """
    Read content from a COS object, decompressing gzip if needed.
    """
    client = make_cos_client(region)
    resp = client.get_object(Bucket=bucket, Key=key)
    
    # Read all chunks to ensure we get the complete file
    body_stream = resp['Body']
    chunks = []
    while True:
        chunk = body_stream.read(8192)  # Read in 8KB chunks
        if not chunk:
            break
        chunks.append(chunk)
    body = b''.join(chunks)
    
    if key.endswith('.gz'):
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
    return body.decode('utf-8', errors='ignore')


def parse_lines(content: str) -> List[Dict[str, Any]]:
    """
    Parse CloudAudit events from JSON or JSON Lines format.
    Supports:
    - Single JSON object (one event per file)
    - JSON Lines (multiple events, one per line)
    """
    items = []
    content = content.strip()
    if not content:
        return items
    
    # Try parsing as single JSON object first
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            items.append(obj)
            return items
        elif isinstance(obj, list):
            return [o for o in obj if isinstance(o, dict)]
    except Exception:
        pass
    
    # Fall back to JSON Lines format (one event per line)
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                items.append(obj)
        except Exception:
            continue
    
    return items


def normalize_key_for_prefix(key: str, bucket: str) -> str:
    """
    Remove leading APPID/bucket prefixes from COS key.
    """
    k = key.lstrip("/")
    parts = k.split("/")
    shortb = bucket.split("-")[0]
    if len(parts) >= 3 and parts[0].isdigit() and parts[1] in (shortb, bucket):
        return "/".join(parts[2:])
    return k


def should_tag(rec: Dict[str, Any]) -> bool:
    """
    Decide if an event is a resource creation we want to tag.
    Supports CVM instances (RunInstances) and CDH hosts (AllocateHosts).
    """
    op = (rec.get("eventName") or rec.get("operationName") or rec.get("action") or "").lower()
    return ("create" in op) or (op in ("runinstances", "createinstance", "createcluster", "allocatehosts"))


def main_handler(event, context):
    """
    SCF handler entry point.
    """
    if not COS_BUCKET or not COS_REGION:
        return {"status": "error", "error": "COS_BUCKET and COS_REGION must be set"}

    # Ensure COS bucket and CloudAudit tracks for all monitored regions
    setup_ok = ensure_cos_bucket(COS_BUCKET, COS_REGION)
    track_ids = {}
    if os.getenv("AUDIT_SETUP", "true").lower() != "false":
        track_ids = ensure_audit_tracks_all_regions(COS_REGION, COS_BUCKET)

    setup_status = {"cos_bucket_ok": setup_ok, "track_ids": track_ids, "monitored_regions": list(track_ids.keys())}

    records  = event.get("Records") or []
    processed, tagged = 0, 0
    errors = []

    for record in records:
        cos      = record.get("cos", {})
        bucket   = cos.get("cosBucket", {}).get("name")   or COS_BUCKET
        key      = cos.get("cosObject", {}).get("key")
        region   = cos.get("cosBucket", {}).get("region") or COS_REGION
        if not bucket or not key or not region:
            errors.append({"error": "missing bucket/key/region", "record": record})
            continue

        expected_prefix   = COS_BASE_PREFIX or ""
        normalized_key    = normalize_key_for_prefix(key, bucket)
        if expected_prefix and not normalized_key.startswith(expected_prefix + "/"):
            continue

        try:
            body = read_cos_object(COS_BUCKET, normalized_key, COS_REGION)
            audit_recs = parse_lines(body)
            processed += len(audit_recs)

            for rec in audit_recs:
                # Skip non-dicts, skip cloudaudit service events
                if not isinstance(rec, dict):
                    continue
                if "cloudaudit" in rec.get("eventSource", "").lower():
                    continue
                
                event_name = rec.get("eventName", "")
                
                # Handle CLB events
                if event_name == "CreateLoadBalancer":
                    if handle_clb_tagging(rec):
                        tagged += 1
                    continue
                
                # Handle CBS events
                if event_name in ("CreateCbsStorages", "CreateDisks", "AttachDisks"):
                    if handle_cbs_tagging(rec):
                        tagged += 1
                    continue
                
                # Handle CVM/CDH events
                if not should_tag(rec):
                    continue

                owner      = get_owner(rec)
                res_region = extract_region(rec) or region
                qcs        = extract_qcs(rec)

                if res_region and qcs:
                    try:
                        tag_resource_qcs(res_region, qcs, build_tags(owner))
                        tagged += 1
                        
                        # Tag CBS disks attached to this CVM (system disk + any data disks)
                        # These disks don't generate separate CreateCbsStorages events
                        if rec.get("eventName") == "RunInstances":
                            # Extract instance_id from resourceSet or QCS
                            instance_id = None
                            resource_set = rec.get("resourceSet", [])
                            if resource_set and isinstance(resource_set, list):
                                for resource in resource_set:
                                    if isinstance(resource, dict) and "Instance" in resource.get("resourceTypeClass", ""):
                                        instance_id = resource.get("resourceId")
                                        break
                            
                            # Fallback: extract from responseElements
                            if not instance_id:
                                resp_str = rec.get("responseElements", "")
                                if resp_str and "InstanceIdSet" in resp_str:
                                    try:
                                        resp_data = json.loads(resp_str)
                                        ids = resp_data.get("InstanceIdSet", [])
                                        if ids:
                                            instance_id = ids[0]
                                    except Exception:
                                        pass
                            
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
                        
                    except Exception as te:
                        print(json.dumps({"error": "tagging_failed", "qcs": qcs, "region": res_region, "message": str(te)}))
                        errors.append({"err": str(te), "qcs": qcs, "region": res_region})
        except Exception as e:
            print(json.dumps({"error": "processing_failed", "bucket": bucket, "key": key, "message": str(e)}))
            errors.append({"error": str(e), "bucket": bucket, "key": key, "region": region})

    return {
        "status":    "ok",
        "setup":     setup_status,
        "processed": processed,
        "tagged":    tagged,
        "errors":    errors
    }


if __name__ == "__main__":
    # Local test
    print(json.dumps(main_handler({"Records": []}, None)))
