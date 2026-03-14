#!/usr/bin/env python3
"""
SCF Resource Tagger

Automatically tags newly created Tencent Cloud resources based on CloudAudit events.
Processes CloudAudit logs delivered to COS and applies standardized tags.

Supported services:
- CVM instances (RunInstances)
- CDH dedicated hosts (AllocateHosts)
- CLB load balancers (CreateLoadBalancer)
- CBS disks (CreateCbsStorages, CreateDisks, AttachDisks)
- EIP elastic IPs (AllocateAddresses)

CloudAudit tracks are global and automatically monitor all regions.

Author: Tudor Toma
Version: 2.1.0
License: Apache 2.0
"""

import os
import sys
import json
import gzip
import re
import traceback
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

# Service handlers
from services.cvm import handle_cvm_tagging, should_tag
from services.clb import handle_clb_tagging
from services.cbs import handle_cbs_tagging
from services.eip import handle_eip_tagging

# Configuration from environment variables
COS_BUCKET       = os.getenv("COS_BUCKET")
COS_REGION       = os.getenv("COS_REGION")
COS_PREFIX       = (os.getenv("COS_PREFIX") or "").strip().rstrip("/")
COS_BASE_PREFIX  = (os.getenv("COS_BASE_PREFIX") or "cloudaudit").strip().rstrip("/")


# ---------------------------------------------------------------------------
# Shared utilities — used by index.py and imported by service modules
# ---------------------------------------------------------------------------

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


def _region_from_event_source(event_source: str) -> Optional[str]:
    """
    Extract region from eventSource hostname.
    Format: service.REGION.api.tencentyun.com  (e.g. vpc.eu-frankfurt.api.tencentyun.com)
    """
    if not event_source:
        return None
    m = re.match(r"^[^.]+\.([^.]+)\.api\.tencentyun\.com$", event_source)
    return m.group(1) if m else None


def extract_region(rec: Dict[str, Any]) -> Optional[str]:
    """
    Extract region information from a CloudAudit record.
    
    Priority:
    1. resourceRegion from resourceSet (most accurate)
    2. Top-level region / requestRegion fields
    3. eventSource hostname region (actual API endpoint used)
    4. eventRegion (CloudAudit processing region — least reliable)
    """
    resource_set = rec.get("resourceSet", [])
    if isinstance(resource_set, list):
        for resource in resource_set:
            if isinstance(resource, dict):
                rr = resource.get("resourceRegion")
                if rr:
                    return rr

    r = rec.get("region") or rec.get("requestRegion")
    if r:
        return r

    es_region = _region_from_event_source(rec.get("eventSource", ""))
    if es_region:
        return es_region

    return rec.get("eventRegion")


def extract_account_uin(rec: Dict[str, Any]) -> str:
    """
    Extract account UIN from a CloudAudit record.
    """
    user_id = rec.get("userIdentity", {})
    if isinstance(user_id, dict):
        return user_id.get("accountId") or user_id.get("principalId") or user_id.get("ownerUin") or ""
    return ""


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


# ---------------------------------------------------------------------------
# COS reading + CloudAudit event parsing
# ---------------------------------------------------------------------------

def read_cos_object(bucket: str, key: str, region: str) -> str:
    """
    Read content from a COS object, decompressing gzip if needed.
    """
    client = make_cos_client(region)
    resp = client.get_object(Bucket=bucket, Key=key)

    body_stream = resp['Body']
    chunks = []
    while True:
        chunk = body_stream.read(8192)
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
    """
    items = []
    content = content.strip()
    if not content:
        return items

    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            items.append(obj)
            return items
        elif isinstance(obj, list):
            return [o for o in obj if isinstance(o, dict)]
    except Exception:
        pass

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


# ---------------------------------------------------------------------------
# CloudAudit track setup
# ---------------------------------------------------------------------------

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
    Creates separate tracks per service type delivering to COS.
    
    Architecture:
        - Track 1 (tagger-cvm-track): ResourceType="cvm" → RunInstances, AllocateHosts
        - Track 2 (tagger-clb-track): ResourceType="clb" → CreateLoadBalancer
        - Track 3 (tagger-cbs-track): ResourceType="cbs" → ["*"] (all CBS write events)
        - Track 4 (tagger-eip-track): ResourceType="eip" → AllocateAddresses
        - All tracks deliver to same COS bucket → single SCF function processes all events
    """
    if not bucket_region or not bucket:
        print(json.dumps({"step": "audit_track", "error": "missing bucket_region or bucket"}))
        return None

    CLOUDAUDIT_API_REGION = "eu-frankfurt"
    client = make_tc_client("cloudaudit", cloudaudit_client.CloudauditClient, CLOUDAUDIT_API_REGION)
    if client is None:
        print(json.dumps({"step": "audit_track", "error": "no SDK credentials available", "api_region": CLOUDAUDIT_API_REGION}))
        return None

    # Track definitions
    tracks_config = [
        {"name": "tagger-cvm-track", "resource_type": "cvm", "event_names": ["RunInstances", "AllocateHosts"]},
        {"name": "tagger-clb-track", "resource_type": "clb", "event_names": ["CreateLoadBalancer"]},
        {"name": "tagger-cbs-track", "resource_type": "cbs", "event_names": ["*"]},
        {"name": "tagger-eip-track", "resource_type": "vpc", "event_names": ["AllocateAddresses"]},
    ]

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
            # Also verify the storage prefix matches
            track_storage = getattr(tr, "Storage", None)
            track_prefix = getattr(track_storage, "StoragePrefix", "") if track_storage else ""
            expected_prefix = prefix_base
            prefix_ok = (track_prefix == expected_prefix)
            if track_status == 1 and track_rt == resource_type and track_evts == sorted(event_names) and prefix_ok:
                print(json.dumps({"step": track_name, "status": "exists", "track_id": track_id,
                                  "action": "skip_recreation", "event_names": track_evts,
                                  "prefix": track_prefix}))
                track_valid = True
            else:
                print(json.dumps({"step": track_name, "status": "needs_update", "track_id": track_id,
                                  "current_status": track_status, "current_rt": track_rt,
                                  "current_events": track_evts, "current_prefix": track_prefix,
                                  "desired_events": sorted(event_names), "desired_prefix": expected_prefix}))

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

    first_track_id = None
    for tc in tracks_config:
        tid = _ensure_track(tc["name"], tc["resource_type"], tc["event_names"])
        if tid and first_track_id is None:
            first_track_id = tid

    return first_track_id


def ensure_audit_tracks_all_regions(bucket_region: str, bucket: str) -> Dict[str, Optional[str]]:
    """
    Ensure global CloudAudit tracks exist.
    CloudAudit tracks automatically monitor all regions by default.
    """
    print(json.dumps({
        "step": "audit_setup",
        "cos_bucket_region": bucket_region,
        "note": "CloudAudit tracks monitor all regions globally"
    }))

    track_id = ensure_audit_track_to_cos(bucket_region, bucket)
    return {"global": track_id}


# ---------------------------------------------------------------------------
# Main SCF handler — event routing
# ---------------------------------------------------------------------------

def main_handler(event, context):
    """
    SCF handler entry point.
    Routes CloudAudit events to the appropriate service handler.
    """
    if not COS_BUCKET or not COS_REGION:
        return {"status": "error", "error": "COS_BUCKET and COS_REGION must be set"}

    # Ensure COS bucket and CloudAudit tracks
    setup_ok = ensure_cos_bucket(COS_BUCKET, COS_REGION)
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
                if not isinstance(rec, dict):
                    continue
                if "cloudaudit" in rec.get("eventSource", "").lower():
                    continue

                event_name = rec.get("eventName", "")

                # --- EIP events ---
                if event_name == "AllocateAddresses":
                    if handle_eip_tagging(rec):
                        tagged += 1
                    continue

                # --- CLB events ---
                if event_name == "CreateLoadBalancer":
                    if handle_clb_tagging(rec):
                        tagged += 1
                    continue

                # --- CBS events ---
                if event_name in ("CreateCbsStorages", "CreateDisks", "AttachDisks"):
                    if handle_cbs_tagging(rec):
                        tagged += 1
                    continue

                # --- CVM/CDH events ---
                if not should_tag(rec):
                    continue

                result = handle_cvm_tagging(rec)
                tagged += result

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
