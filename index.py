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

import json
import datetime
import traceback
import re
import gzip
from typing import List, Dict, Any, Optional

# Tencent Cloud SDKs
from qcloud_cos import CosConfig, CosS3Client
from tencentcloud.common import credential
from tencentcloud.tag.v20180813 import tag_client, models as tag_models
from tencentcloud.cloudaudit.v20190319 import cloudaudit_client, models as audit_models

# Configuration from environment variables
COS_BUCKET = os.getenv("COS_BUCKET")
COS_REGION = os.getenv("COS_REGION")
COS_PREFIX = (os.getenv("COS_PREFIX") or "").strip().rstrip("/")
COS_BASE_PREFIX = (os.getenv("COS_BASE_PREFIX") or "cloudaudit").strip().rstrip("/")


def region_short(region: str) -> str:
    """
    Convert region name to short form for naming conventions.
    
    Args:
        region: Full region name (e.g., 'eu-frankfurt')
        
    Returns:
        Short region code (e.g., 'fra')
    """
    if not isinstance(region, str) or not region:
        return "unk"
    parts = region.split("-")
    tail = parts[-1] if parts else region
    return tail[:3].lower()


def _build_cred():
    """
    Build TencentCloud credentials from environment variables or role metadata.
    
    Returns:
        Credential object or None if no credentials available
    """
    # Prefer environment variables (injected by SCF execution role)
    sid = os.getenv("TENCENTCLOUD_SECRETID") or os.getenv("TENCENTCLOUD_SECRET_ID")
    sk = os.getenv("TENCENTCLOUD_SECRETKEY") or os.getenv("TENCENTCLOUD_SECRET_KEY")
    tok = os.getenv("TENCENTCLOUD_SESSIONTOKEN") or os.getenv("TENCENTCLOUD_SESSION_TOKEN")
    
    if sid and sk:
        return credential.Credential(sid, sk, tok or "")
    
    # Fallback to CVM role metadata
    try:
        cred = credential.CVMRoleCredential()
        # Validate credentials are available
        if getattr(cred, "get_secret_id")() and getattr(cred, "get_secret_key")():
            return cred
    except Exception:
        pass
    
    return None


def make_tc_client(service_key: str, client_cls, region: str):
    """
    Create a TencentCloud API client with proper configuration.
    
    Args:
        service_key: Service identifier (e.g., 'cloudaudit', 'tag')
        client_cls: Client class to instantiate
        region: Target region for the client
        
    Returns:
        Configured client instance or None if credentials unavailable
    """
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    
    # Configure HTTP profile
    http_profile = HttpProfile()
    http_profile.endpoint = f"{service_key}.tencentcloudapi.com"
    client_profile = ClientProfile(httpProfile=http_profile)
    
    # Get credentials
    cred = _build_cred()
    if not cred:
        try:
            print(json.dumps({"step": "tc_sdk_creds", "present": False}))
        except Exception:
            pass
        return None
    
    try:
        print(json.dumps({"step": "tc_sdk_creds", "present": True}))
    except Exception:
        pass
    
    return client_cls(cred, region, client_profile)


def make_cos_client(region: str) -> CosS3Client:
    """
    Create a COS client for accessing object storage.
    
    Args:
        region: COS region
        
    Returns:
        Configured COS client
        
    Raises:
        RuntimeError: If credentials are not available
    """
    sid = os.getenv("TENCENTCLOUD_SECRETID") or os.getenv("TENCENTCLOUD_SECRET_ID")
    sk = os.getenv("TENCENTCLOUD_SECRETKEY") or os.getenv("TENCENTCLOUD_SECRET_KEY")
    tok = os.getenv("TENCENTCLOUD_SESSIONTOKEN") or os.getenv("TENCENTCLOUD_SESSION_TOKEN")
    
    if not sid or not sk:
        raise RuntimeError(
            "COS credentials not found in environment. "
            "Ensure the SCF role provides TENCENTCLOUD_SECRETID/SECRETKEY/SESSIONTOKEN."
        )
    
    cfg = CosConfig(Region=region, SecretId=sid, SecretKey=sk, Token=tok or "")
    return CosS3Client(cfg)


def ensure_cos_bucket(bucket: str, region: str) -> bool:
    """
    Ensure the COS bucket exists, creating it if necessary.
    
    Args:
        bucket: Bucket name
        region: Bucket region
        
    Returns:
        True if bucket exists or was created successfully
    """
    client = make_cos_client(region)
    
    try:
        client.head_bucket(Bucket=bucket)
        print(json.dumps({
            "step": "cos_bucket", 
            "status": "exists", 
            "bucket": bucket, 
            "region": region
        }))
        return True
    except Exception:
        try:
            client.create_bucket(Bucket=bucket)
            print(json.dumps({
                "step": "cos_bucket", 
                "status": "created", 
                "bucket": bucket, 
                "region": region
            }))
            return True
        except Exception as e:
            print(json.dumps({
                "step": "cos_bucket", 
                "error": str(e), 
                "bucket": bucket, 
                "region": region
            }))
            return False


def ensure_audit_track_to_cos(bucket_region: str, bucket: str) -> Optional[str]:
    """
    Ensure CloudAudit track exists and is configured to deliver logs to COS.
    
    Creates or updates a CloudAudit track with the following configuration:
    - Track name: {region_short}-tagger-track
    - Events: Only RunInstances from CVM service
    - Storage: COS bucket with cloudaudit prefix
    
    Args:
        bucket_region: Region where the COS bucket is located
        bucket: COS bucket name for storing audit logs
        
    Returns:
        Track ID if successful, None otherwise
    """
    if not bucket_region or not bucket:
        print(json.dumps({
            "step": "audit_track", 
            "error": "missing bucket_region or bucket", 
            "bucket_region": bucket_region, 
            "bucket": bucket
        }))
        return None
    
    try:
        print(json.dumps({
            "step": "audit_track_debug_ctx", 
            "bucket": bucket, 
            "bucket_region": bucket_region
        }))
    except Exception:
        pass
    
    region = bucket_region
    client = make_tc_client("cloudaudit", cloudaudit_client.CloudauditClient, region)
    
    if client is None:
        print(json.dumps({
            "step": "audit_track", 
            "error": "no SDK credentials available; skipping CloudAudit setup"
        }))
        return None
    
    # Configure track settings
    track_name = f"{region_short(region)}-tagger-track"
    
    # Configure storage settings
    storage = audit_models.Storage()
    try:
        setattr(storage, "StorageType", "cos")
        setattr(storage, "StorageRegion", bucket_region)
        
        # StorageName must be bucket short name without -APPID
        short_bucket = bucket.split("-")[0] if bucket else bucket
        setattr(storage, "StorageName", short_bucket)
        
        # CloudAudit expects StoragePrefix: 3-40 alphanumeric characters only
        prefix_base = re.sub(r"[^A-Za-z0-9]", "", COS_BASE_PREFIX or "cloudaudit")
        if len(prefix_base) < 3:
            prefix_base = (prefix_base + "logs")[:3]
        setattr(storage, "StoragePrefix", prefix_base[:40])
    except Exception:
        pass
    
    track_id = None
    
    try:
        # Check for existing tracks
        dreq = audit_models.DescribeAuditTracksRequest()
        setattr(dreq, "PageNumber", 1)
        setattr(dreq, "PageSize", 50)
        
        try:
            print(json.dumps({"step": "audit_track_before_describe"}))
        except Exception:
            pass
        
        dresp = client.DescribeAuditTracks(dreq)
        
        try:
            print(json.dumps({"step": "audit_track_after_describe"}))
        except Exception:
            pass
        
        # Find existing track
        existing = None
        for tr in getattr(dresp, "Tracks", []) or []:
            if getattr(tr, "Name", "") == track_name:
                existing = tr
                track_id = getattr(tr, "TrackId", None)
                break

        def cfg_matches(tr) -> bool:
            """Check if track configuration matches our requirements."""
            try:
                return (
                    getattr(tr, "Storage", None) == "cos" and
                    getattr(tr, "StorageRegion", None) == bucket_region and
                    (getattr(tr, "StorageBucket", None) == bucket or 
                     getattr(tr, "StorageName", None) == bucket) and
                    (getattr(tr, "StoragePrefix", "") or "") == 
                    (f"{COS_BASE_PREFIX}/{bucket_region}" if COS_BASE_PREFIX else bucket_region)
                )
            except Exception:
                return False

        # Update existing track if configuration doesn't match
        if existing and track_id:
            if not cfg_matches(existing):
                try:
                    mreq = audit_models.ModifyAuditTrackRequest()
                    setattr(mreq, "TrackId", track_id)
                    setattr(mreq, "Name", track_name)
                    setattr(mreq, "Status", 1)
                    
                    # Configure event filtering - only CVM RunInstances events
                    try:
                        setattr(mreq, "ActionType", "Write")
                    except Exception:
                        pass
                    try:
                        setattr(mreq, "ResourceType", "cvm")
                    except Exception:
                        pass
                    try:
                        setattr(mreq, "EventNames", ["RunInstances"])
                    except Exception:
                        pass
                    
                    # Debug storage configuration
                    try:
                        print(json.dumps({
                            "region": region, 
                            "step": "audit_track_storage_modify", 
                            "storage": storage
                        }))
                    except Exception:
                        pass
                    
                    setattr(mreq, "Storage", storage)
                    client.ModifyAuditTrack(mreq)
                    
                    print(json.dumps({
                        "region": region, 
                        "step": "audit_track", 
                        "status": "updated", 
                        "track_id": track_id
                    }))
                except Exception as e:
                    print(json.dumps({
                        "region": region, 
                        "step": "audit_track_update_attempt", 
                        "error": str(e)
                    }))
            else:
                print(json.dumps({
                    "region": region, 
                    "step": "audit_track", 
                    "status": "exists", 
                    "track_id": track_id
                }))
        else:
            # Create new track
            try:
                creq = audit_models.CreateAuditTrackRequest()
                setattr(creq, "Name", track_name)
                setattr(creq, "Status", 1)
                
                # Configure event filtering
                try:
                    setattr(creq, "ActionType", "Write")
                except Exception:
                    pass
                try:
                    setattr(creq, "ResourceType", "cvm")
                except Exception:
                    pass
                try:
                    setattr(creq, "EventNames", ["RunInstances"])
                except Exception:
                    pass
                
                setattr(creq, "Storage", storage)
                cresp = client.CreateAuditTrack(creq)
                track_id = getattr(cresp, "TrackId", None)
                
                print(json.dumps({
                    "region": region, 
                    "step": "audit_track", 
                    "status": "created", 
                    "track_id": track_id
                }))
            except Exception as e:
                print(json.dumps({
                    "region": region, 
                    "step": "audit_track_create_attempt", 
                    "error": str(e)
                }))
    
    except Exception as e:
        tb = traceback.format_exc()
        print(json.dumps({
            "region": region,
            "step": "audit_track",
            "type": e.__class__.__name__,
            "code": getattr(e, "code", None),
            "message": getattr(e, "message", str(e)),
            "requestId": getattr(e, "requestId", None),
            "traceback": tb
        }))
    
    return track_id


def build_tags(owner: str) -> List[Dict[str, str]]:
    """
    Build standardized tags for resources.
    
    Args:
        owner: Resource owner identifier
        
    Returns:
        List of tag dictionaries with TagKey and TagValue
    """
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner", "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated", "TagValue": today},
        {"TagKey": "TaggerLifeDays", "TagValue": "1"},
        {"TagKey": "TaggerAutoOff", "TagValue": "YES"},
        {"TagKey": "TaggerProject", "TagValue": "n/a"},
    ]


def get_owner(rec: Dict[str, Any]) -> str:
    """
    Extract owner information from CloudAudit record.
    
    Prioritizes identification in this order:
    1. Email address (most user-friendly)
    2. User name (human-readable)
    3. Account ID (more readable than UIN)
    4. UIN (fallback)
    
    Args:
        rec: CloudAudit event record
        
    Returns:
        Owner identifier string
    """
    ui = rec.get("userIdentity") or rec.get("user", {}) or {}
    
    # Ensure userIdentity is a dictionary
    if not isinstance(ui, dict):
        return "unknown"
    
    # Priority 1: Email address
    email = ui.get("userEmail") or ui.get("email")
    if email:
        return email
    
    # Priority 2: User name
    user_name = ui.get("userName") or ui.get("name") or ui.get("displayName")
    if user_name:
        return user_name
    
    # Priority 3: Account ID (more readable than UIN)
    account_id = ui.get("accountId")
    if account_id:
        return f"account:{account_id}"
    
    # Fallback: UIN
    uin = ui.get("uin") or ui.get("principalId") or ui.get("ownerUin")
    return f"uin:{uin}" if uin else "unknown"


def make_tag_client(region: str):
    """Create a Tag API client for the specified region."""
    cred = _build_cred()
    return tag_client.TagClient(cred, region)


def tag_resource_qcs(region: str, qcs: str, tags: List[Dict[str, str]]) -> None:
    """
    Apply tags to a resource using its QCS identifier.
    
    Args:
        region: Resource region
        qcs: Qualified Cloud Service identifier
        tags: List of tags to apply
        
    Raises:
        Exception: If tagging fails
    """
    client = make_tag_client(region)
    req = tag_models.TagResourcesRequest()
    req.ResourceList = [qcs]
    req.Tags = tags
    client.TagResources(req)


def extract_region(rec: Dict[str, Any]) -> Optional[str]:
    """
    Extract region information from CloudAudit record.
    
    Args:
        rec: CloudAudit event record
        
    Returns:
        Region string or None if not found
    """
    return rec.get("region") or rec.get("requestRegion") or rec.get("eventRegion")


def extract_qcs(rec: Dict[str, Any]) -> Optional[str]:
    """
    Extract or build QCS (Qualified Cloud Service) identifier from CloudAudit record.
    
    QCS format: qcs::service:region:account:resource
    Example: qcs::cvm:eu-frankfurt:uin/1301327510:instance/ins-abc123
    
    Args:
        rec: CloudAudit event record
        
    Returns:
        QCS string or None if cannot be determined
    """
    # Check for direct QCS in record
    for key in ("resourceId", "resource", "resourceQcs", "qcs", "targetResource"):
        val = rec.get(key)
        if isinstance(val, str) and val.startswith("qcs::"):
            return val
    
    # Handle CVM RunInstances events specifically
    event_name = rec.get("eventName", "")
    if event_name == "RunInstances":
        # Try extracting from resourceSet first
        resource_set = rec.get("resourceSet", [])
        if resource_set and len(resource_set) > 0:
            resource = resource_set[0]
            
            # Ensure resource is a dictionary
            if isinstance(resource, dict):
                instance_id = resource.get("resourceId")
                resource_region = resource.get("resourceRegion")
                
                # Extract owner UIN with fallback priority
                user_identity = rec.get("userIdentity", {})
                owner_uin = ""
                if isinstance(user_identity, dict):
                    owner_uin = (user_identity.get("accountId") or 
                               user_identity.get("principalId") or 
                               user_identity.get("ownerUin") or "")
                
                if instance_id and resource_region:
                    return f"qcs::cvm:{resource_region}:uin/{owner_uin}:instance/{instance_id}"
        
        # Fallback: try extracting from responseElements
        response_str = rec.get("responseElements", "")
        if response_str and "InstanceIdSet" in response_str:
            try:
                response = json.loads(response_str)
                instance_ids = response.get("InstanceIdSet", [])
                
                if instance_ids:
                    instance_id = instance_ids[0]
                    
                    # Determine resource region
                    resource_region = None
                    if resource_set and isinstance(resource_set[0], dict):
                        resource_region = resource_set[0].get("resourceRegion")
                    
                    if not resource_region:
                        placement = rec.get("requestParameters", {}).get("Placement", {})
                        if isinstance(placement, dict):
                            zone = placement.get("Zone", "")
                            if zone:
                                # Extract region from zone (e.g., eu-frankfurt-1 -> eu-frankfurt)
                                resource_region = "-".join(zone.split("-")[:-1])
                    
                    # Extract owner UIN
                    user_identity = rec.get("userIdentity", {})
                    owner_uin = ""
                    if isinstance(user_identity, dict):
                        owner_uin = (user_identity.get("accountId") or user_identity.get("principalId") or user_identity.get("ownerUin") or 
                                   user_identity.get("accountId") or 
                                   user_identity.get("principalId") or "")
                    
                    if instance_id and resource_region:
                        uin_part = f"uin/{owner_uin}" if owner_uin else ""
                        return f"qcs::cvm:{resource_region}:{uin_part}:instance/{instance_id}"
            except Exception:
                pass
    
    # Generic fallback for other resource types
    service = rec.get("service") or rec.get("eventSource")
    request_params = rec.get("requestParameters", {})
    resource_id = rec.get("resourceId")
    
    if not resource_id and isinstance(request_params, dict):
        resource_id = request_params.get("ResourceId")
    
    region = extract_region(rec)
    
    if service and resource_id and region:
        user_identity = rec.get("userIdentity", {})
        owner_uin = ""
        if isinstance(user_identity, dict):
            owner_uin = user_identity.get("accountId") or user_identity.get("principalId") or user_identity.get("ownerUin") or user_identity.get("accountId") or user_identity.get("principalId") or user_identity.get("ownerUin") or ""
        
        uin_part = f"uin/{owner_uin}" if owner_uin else ""
        return f"qcs::{service}:{region}:{uin_part}:resourceId/{resource_id}"
    
    return None


def read_cos_object(bucket: str, key: str, region: str) -> str:
    """
    Read content from a COS object, handling gzip compression if present.
    
    Args:
        bucket: COS bucket name
        key: Object key
        region: COS region
        
    Returns:
        Object content as string
        
    Raises:
        Exception: If object cannot be read
    """
    client = make_cos_client(region)
    response = client.get_object(Bucket=bucket, Key=key)
    body = response['Body'].read()
    
    # Handle gzip compression
    if key.endswith('.gz'):
        try:
            body = gzip.decompress(body)
        except Exception:
            pass  # Not actually gzipped
    
    return body.decode('utf-8', errors='ignore')


def parse_lines(content: str) -> List[Dict[str, Any]]:
    """
    Parse CloudAudit log content into individual event records.
    
    CloudAudit logs are typically JSON Lines format (one JSON object per line).
    
    Args:
        content: Raw log content
        
    Returns:
        List of parsed event dictionaries
    """
    items = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                items.append(obj)
        except Exception:
            continue  # Skip malformed lines
    
    return items


def normalize_key_for_prefix(key: str, bucket: str) -> str:
    """
    Normalize COS object key by removing bucket-specific prefixes.
    
    CloudAudit may store objects with paths like:
    /APPID/bucket-name/actual/path -> actual/path
    
    Args:
        key: Original object key
        bucket: Bucket name
        
    Returns:
        Normalized key without bucket prefixes
    """
    k = key.lstrip("/")
    parts = k.split("/")
    short_bucket = bucket.split("-")[0] if bucket else bucket
    
    # Remove APPID/bucket prefix if present
    if len(parts) >= 3 and parts[0].isdigit() and (parts[1] == short_bucket or parts[1] == bucket):
        return "/".join(parts[2:])
    
    return k


def should_tag(rec: Dict[str, Any]) -> bool:
    """
    Determine if a CloudAudit record represents a resource creation event that should be tagged.
    
    Args:
        rec: CloudAudit event record
        
    Returns:
        True if the event should trigger tagging
    """
    op = rec.get("eventName") or rec.get("operationName") or rec.get("action")
    if not isinstance(op, str):
        return False
    
    op = op.lower()
    return ("create" in op) or (op in ("runinstances", "createinstance", "createcluster"))


def main_handler(event, context):
    """
    Main SCF handler function.
    
    Processes COS trigger events containing CloudAudit logs and tags newly created resources.
    
    Args:
        event: SCF trigger event (COS object creation)
        context: SCF runtime context
        
    Returns:
        Dictionary with processing results and statistics
    """
    # Initialize setup
    setup_ok = True
    
    # Validate required configuration
    if not COS_BUCKET or not COS_REGION:
        return {
            "status": "error", 
            "error": "COS_BUCKET and COS_REGION must be set"
        }
    
    # Ensure COS bucket exists
    if not ensure_cos_bucket(COS_BUCKET, COS_REGION):
        setup_ok = False
    
    # Setup CloudAudit track if enabled
    track_id = None
    if os.getenv("AUDIT_SETUP", "true").lower() != "false":
        track_id = ensure_audit_track_to_cos(COS_REGION, COS_BUCKET)
    
    setup_status = {"cos_bucket_ok": setup_ok, "track_id": track_id}
    
    # Process COS trigger records
    records = event.get("Records") or []
    processed = 0
    tagged = 0
    errors = []
    
    for record in records:
        cos = record.get("cos", {})
        bucket = cos.get("cosBucket", {}).get("name") or COS_BUCKET
        key = cos.get("cosObject", {}).get("key")
        region = cos.get("cosBucket", {}).get("region") or COS_REGION
        
        # Validate record data
        if not bucket or not key or not region:
            errors.append({
                "error": "missing bucket/key/region", 
                "record": record
            })
            continue
        
        # Check if object matches expected prefix
        expected_prefix = COS_BASE_PREFIX or ""
        normalized_key = normalize_key_for_prefix(key, bucket)
        
        try:
            print(json.dumps({
                "step": "record", 
                "bucket": bucket, 
                "region": region, 
                "key": key, 
                "normalized_key": normalized_key, 
                "expected_prefix": expected_prefix
            }))
        except Exception:
            pass
        
        # Skip objects that don't match the expected prefix
        if expected_prefix and not normalized_key.startswith(expected_prefix + "/"):
            try:
                print(json.dumps({
                    "step": "record_skip_prefix", 
                    "key": key, 
                    "normalized_key": normalized_key, 
                    "expected_prefix": expected_prefix
                }))
            except Exception:
                pass
            continue
        
        try:
            # Read and parse CloudAudit log content
            body = read_cos_object(COS_BUCKET, normalized_key, COS_REGION)
            
            try:
                print(json.dumps({
                    "step": "record_body", 
                    "bucket_used": COS_BUCKET, 
                    "region_used": COS_REGION, 
                    "key": normalized_key, 
                    "size": len(body)
                }))
            except Exception:
                pass
            
            # Parse log entries
            audit_records = parse_lines(body)
            
            try:
                first_event = None
                if audit_records:
                    first_event = (audit_records[0].get("eventName") or 
                                 audit_records[0].get("operationName") or 
                                 audit_records[0].get("action"))
                
                print(json.dumps({
                    "step": "record_parsed", 
                    "key": key, 
                    "count": len(audit_records), 
                    "first_event": first_event
                }))
            except Exception:
                pass
            
            processed += len(audit_records)
            
            # Process each audit record
            for audit_rec in audit_records:
                # Debug logging for troubleshooting
                try:
                    rec_type = type(audit_rec).__name__
                    rec_keys = list(audit_rec.keys())[:5] if hasattr(audit_rec, 'keys') else []
                    event_name = audit_rec.get("eventName", "")
                    resource_set = audit_rec.get("resourceSet", [])
                    user_identity = audit_rec.get("userIdentity", {})
                    user_identity_type = type(user_identity).__name__
                    
                    # Extract owner UIN for debugging
                    owner_uin = ""
                    if isinstance(user_identity, dict):
                        owner_uin = (user_identity.get("accountId") or user_identity.get("principalId") or user_identity.get("ownerUin") or 
                                   user_identity.get("accountId") or 
                                   user_identity.get("principalId") or "")
                    
                    # Extract resource ID for debugging
                    resource_id = ""
                    if resource_set and isinstance(resource_set[0], dict):
                        resource_id = resource_set[0].get("resourceId", "")
                    
                    print(json.dumps({
                        "step": "record_debug", 
                        "key": key, 
                        "rec_type": rec_type, 
                        "rec_keys": rec_keys, 
                        "event_name": event_name, 
                        "resource_set_count": len(resource_set), 
                        "user_identity_type": user_identity_type, 
                        "owner_uin": owner_uin, 
                        "resource_id": resource_id, 
                        "user_identity_keys": list(user_identity.keys()) if isinstance(user_identity, dict) else []
                    }))
                except Exception as e:
                    print(json.dumps({
                        "step": "record_debug_error", 
                        "key": key, 
                        "error": str(e)
                    }))
                
                # Validate record type
                if not isinstance(audit_rec, dict):
                    try:
                        print(json.dumps({
                            "step": "record_skip_type", 
                            "key": key, 
                            "type": type(audit_rec).__name__
                        }))
                    except Exception:
                        pass
                    continue
                
                # Skip CloudAudit service events (including self-generated ones)
                event_source = audit_rec.get("eventSource", "")
                if "cloudaudit" in event_source.lower():
                    try:
                        print(json.dumps({
                            "step": "record_skip_cloudaudit", 
                            "key": key, 
                            "op": (audit_rec.get("eventName") or 
                                  audit_rec.get("operationName") or 
                                  audit_rec.get("action"))
                        }))
                    except Exception:
                        pass
                    continue
                
                # Check if this event should trigger tagging
                if not should_tag(audit_rec):
                    try:
                        print(json.dumps({
                            "step": "record_skip_op", 
                            "key": key, 
                            "op": (audit_rec.get("eventName") or 
                                  audit_rec.get("operationName") or 
                                  audit_rec.get("action"))
                        }))
                    except Exception:
                        pass
                    continue
                
                # Extract tagging information
                owner = get_owner(audit_rec)
                res_region = extract_region(audit_rec) or region
                qcs = extract_qcs(audit_rec)
                
                # Debug tagging attempt
                try:
                    print(json.dumps({
                        "step": "tag_attempt", 
                        "key": key, 
                        "owner": owner, 
                        "res_region": res_region, 
                        "qcs": qcs, 
                        "has_both": bool(res_region and qcs)
                    }))
                except Exception:
                    pass
                
                # Apply tags if we have all required information
                if res_region and qcs:
                    try:
                        tag_resource_qcs(res_region, qcs, build_tags(owner))
                        tagged += 1
                        
                        try:
                            print(json.dumps({
                                "step": "tag_success", 
                                "key": key, 
                                "qcs": qcs, 
                                "region": res_region
                            }))
                        except Exception:
                            pass
                    except Exception as te:
                        errors.append({
                            "err": str(te), 
                            "qcs": qcs, 
                            "region": res_region
                        })
                        try:
                            print(json.dumps({
                                "step": "tag_error", 
                                "key": key, 
                                "error": str(te), 
                                "qcs": qcs, 
                                "region": res_region
                            }))
                        except Exception:
                            pass
        
        except Exception as e:
            errors.append({
                "error": str(e), 
                "bucket": bucket, 
                "key": key, 
                "region": region
            })
    
    return {
        "status": "ok", 
        "setup": setup_status, 
        "processed": processed, 
        "tagged": tagged, 
        "errors": errors
    }


if __name__ == "__main__":
    # For local testing
    print(json.dumps(main_handler({"Records": []}, None)))