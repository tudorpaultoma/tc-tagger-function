#!/usr/bin/env python3
"""
SCF Resource Tagger

Automatically tags newly created CVM instances based on CloudAudit events.
This SCF function processes CloudAudit logs delivered to COS and applies
standardized tags to resources for better management and cost tracking.

Author: Tudor Toma
Version: 1.0.0
License: Apache 2.0
"""

import os
import sys
import json
import datetime
import traceback
import re
import gzip
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

# Configuration from environment variables
COS_BUCKET      = os.getenv("COS_BUCKET")
COS_REGION      = os.getenv("COS_REGION")
COS_PREFIX      = (os.getenv("COS_PREFIX") or "").strip().rstrip("/")
COS_BASE_PREFIX = (os.getenv("COS_BASE_PREFIX") or "cloudaudit").strip().rstrip("/")


def region_short(region: str) -> str:
    """
    Convert region name to short form for naming conventions.
    e.g. 'eu-frankfurt' -> 'fra'
    """
    if not isinstance(region, str) or not region:
        return "unk"
    parts = region.split("-")
    tail = parts[-1] if parts else region
    return tail[:3].lower()


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
        print(json.dumps({"step": "tc_sdk_creds", "present": False}))
        return None
    print(json.dumps({"step": "tc_sdk_creds", "present": True}))
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
    Ensure a CloudAudit track exists that delivers CVM RunInstances events to COS.
    Returns the TrackId if successful.
    """
    if not bucket_region or not bucket:
        print(json.dumps({"step": "audit_track", "error": "missing bucket_region or bucket"}))
        return None

    client = make_tc_client("cloudaudit", cloudaudit_client.CloudauditClient, bucket_region)
    if client is None:
        print(json.dumps({"step": "audit_track", "error": "no SDK credentials available"}))
        return None

    track_name = f"{region_short(bucket_region)}-tagger-track"
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

    track_id = None
    try:
        # List existing tracks
        dreq = audit_models.DescribeAuditTracksRequest()
        setattr(dreq, "PageNumber", 1)
        setattr(dreq, "PageSize", 50)
        dresp = client.DescribeAuditTracks(dreq)

        existing = None
        for tr in getattr(dresp, "Tracks", []) or []:
            if getattr(tr, "Name", "") == track_name:
                existing = tr
                track_id = getattr(tr, "TrackId", None)
                break

        def cfg_matches(tr) -> bool:
            return (
                getattr(tr, "Storage", None) == "cos"
                and getattr(tr, "StorageRegion", None) == bucket_region
                and (getattr(tr, "StorageBucket", None) == bucket or getattr(tr, "StorageName", None) == bucket)
                and (getattr(tr, "StoragePrefix", "") or "") == f"{COS_BASE_PREFIX}/{bucket_region}"
            )

        if existing and track_id:
            if not cfg_matches(existing):
                # Update track
                mreq = audit_models.ModifyAuditTrackRequest()
                setattr(mreq, "TrackId", track_id)
                setattr(mreq, "Name", track_name)
                setattr(mreq, "Status", 1)
                setattr(mreq, "ActionType", "Write")
                setattr(mreq, "ResourceType", "cvm")
                setattr(mreq, "EventNames", ["RunInstances"])
                setattr(mreq, "Storage", storage)
                client.ModifyAuditTrack(mreq)
                print(json.dumps({"step": "audit_track", "status": "updated", "track_id": track_id}))
            else:
                print(json.dumps({"step": "audit_track", "status": "exists", "track_id": track_id}))
        else:
            # Create new track
            creq = audit_models.CreateAuditTrackRequest()
            setattr(creq, "Name", track_name)
            setattr(creq, "Status", 1)
            setattr(creq, "ActionType", "Write")
            setattr(creq, "ResourceType", "cvm")
            setattr(creq, "EventNames", ["RunInstances"])
            setattr(creq, "Storage", storage)
            cresp = client.CreateAuditTrack(creq)
            track_id = getattr(cresp, "TrackId", None)
            print(json.dumps({"step": "audit_track", "status": "created", "track_id": track_id}))
    except Exception as e:
        tb = traceback.format_exc()
        print(json.dumps({
            "step": "audit_track",
            "error": str(e),
            "traceback": tb
        }))
    return track_id


def build_tags(owner: str) -> List[Dict[str, str]]:
    """
    Build standardized tags for resources.
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",     "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",   "TagValue": today},
        {"TagKey": "TaggerLifeDays",  "TagValue": "1"},
        {"TagKey": "TaggerAutoOff",   "TagValue": "YES"},
        {"TagKey": "TaggerAutoStart", "TagValue": "NO"},
        {"TagKey": "TaggerTTL",       "TagValue": "7"},
        {"TagKey": "TaggerProject",   "TagValue": "n/a"},
    ]


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
    client.TagResources(req)


def extract_region(rec: Dict[str, Any]) -> Optional[str]:
    """
    Extract region information from a CloudAudit record.
    """
    return rec.get("region") or rec.get("requestRegion") or rec.get("eventRegion")


def extract_qcs(rec: Dict[str, Any]) -> Optional[str]:
    """
    Build QCS identifier for a CVM instance creation event.
    """
    # Direct QCS field?
    for key in ("resourceId", "resource", "resourceQcs", "qcs", "targetResource"):
        val = rec.get(key)
        if isinstance(val, str) and val.startswith("qcs::"):
            return val

    # Handle CVM RunInstances events
    evt = rec.get("eventName", "")
    if evt == "RunInstances":
        # Try resourceSet first
        resource_set = rec.get("resourceSet", [])
        if resource_set and isinstance(resource_set[0], dict):
            instance_id    = resource_set[0].get("resourceId")
            resource_region= resource_set[0].get("resourceRegion")
            user_id        = rec.get("userIdentity", {})
            if isinstance(user_id, dict):
                owner_uin = user_id.get("accountId") or user_id.get("principalId") or user_id.get("ownerUin") or ""
            else:
                owner_uin = ""
            if instance_id and resource_region:
                return f"qcs::cvm:{resource_region}:uin/{owner_uin}:instance/{instance_id}"

        # Fallback: parse responseElements
        resp_str = rec.get("responseElements", "")
        if resp_str and "InstanceIdSet" in resp_str:
            try:
                resp = json.loads(resp_str)
                ids  = resp.get("InstanceIdSet", [])
                if ids:
                    instance_id = ids[0]
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
    """
    op = (rec.get("eventName") or rec.get("operationName") or rec.get("action") or "").lower()
    return ("create" in op) or (op in ("runinstances", "createinstance", "createcluster"))


def main_handler(event, context):
    """
    SCF handler entry point.
    """
    if not COS_BUCKET or not COS_REGION:
        return {"status": "error", "error": "COS_BUCKET and COS_REGION must be set"}

    # Ensure COS bucket and CloudAudit track
    setup_ok = ensure_cos_bucket(COS_BUCKET, COS_REGION)
    track_id = None
    if os.getenv("AUDIT_SETUP", "true").lower() != "false":
        track_id = ensure_audit_track_to_cos(COS_REGION, COS_BUCKET)

    setup_status = {"cos_bucket_ok": setup_ok, "track_id": track_id}

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
                if not should_tag(rec):
                    continue

                owner      = get_owner(rec)
                res_region = extract_region(rec) or region
                qcs        = extract_qcs(rec)

                if res_region and qcs:
                    try:
                        tag_resource_qcs(res_region, qcs, build_tags(owner))
                        tagged += 1
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
