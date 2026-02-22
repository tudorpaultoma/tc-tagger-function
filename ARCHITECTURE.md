# Architecture: Multi-Track CloudAudit Design

## Overview

This document explains the architectural decision to use **separate CloudAudit tracks per service type** instead of a single global track.

## Background

### Initial Approach (Failed)
Initially attempted to use a single CloudAudit track with:
```json
{
  "ResourceType": "*",  // Wildcard to monitor all services
  "EventNames": ["RunInstances", "AllocateHosts", "CreateLoadBalancer"]
}
```

**Result**: CloudAudit API rejected this with error:
```
[TencentCloudSDKException] code:InvalidParameter message:illegal params. EventNames error
```

### Root Cause
After extensive testing, discovered:
- CloudAudit **does not support** `ResourceType: "*"` (wildcard) when `EventNames` is specified
- `EventNames` is a **required parameter** (cannot be omitted)
- **Conclusion**: Wildcard + EventNames combination is fundamentally incompatible

## Current Architecture

### Multi-Track Approach

**Solution**: Create separate tracks for each service type with specific ResourceType.

```
┌─────────────────────────────────────────────────────────────┐
│                     CloudAudit Service                       │
│                                                              │
│  ┌────────────────────┐        ┌────────────────────┐      │
│  │  Track 1: CVM      │        │  Track 2: CLB      │      │
│  │  ID: 647           │        │  ID: 648           │      │
│  │  ResourceType: cvm │        │  ResourceType: clb │      │
│  │  Events:           │        │  Events:           │      │
│  │  - RunInstances    │        │  - CreateLoadBal.. │      │
│  │  - AllocateHosts   │        │                    │      │
│  └─────────┬──────────┘        └─────────┬──────────┘      │
│            │                              │                  │
└────────────┼──────────────────────────────┼─────────────────┘
             │                              │
             └──────────┬───────────────────┘
                        │
                        ▼
              ┌──────────────────┐
              │   COS Bucket     │
              │  tommywork-xxx   │
              │  cloudaudit/     │
              └────────┬─────────┘
                       │
                       │ COS Trigger
                       │
                       ▼
              ┌──────────────────┐
              │   SCF Function   │
              │  resource-tagger │
              │                  │
              │  Processes all   │
              │  service events  │
              └──────────────────┘
```

### Track Configuration

#### Track 1: CVM/CDH Resources
```json
{
  "Name": "tagger-cvm-track",
  "ResourceType": "cvm",
  "EventNames": ["RunInstances", "AllocateHosts"],
  "ActionType": "Write",
  "Storage": {
    "StorageType": "cos",
    "StorageName": "tommywork",
    "StorageRegion": "eu-frankfurt",
    "StoragePrefix": "cloudaudit"
  }
}
```

#### Track 2: CLB Resources
```json
{
  "Name": "tagger-clb-track",
  "ResourceType": "clb",
  "EventNames": ["CreateLoadBalancer"],
  "ActionType": "Write",
  "Storage": {
    "StorageType": "cos",
    "StorageName": "tommywork",
    "StorageRegion": "eu-frankfurt",
    "StoragePrefix": "cloudaudit"
  }
}
```

## Benefits

### 1. **CloudAudit API Compliance**
- Uses supported API patterns
- No "illegal params" errors
- Reliable event delivery

### 2. **Separation of Concerns**
- Each track has clear responsibility
- Easier to debug service-specific issues
- Independent configuration per service

### 3. **Scalability**
- Easy to add new service types
- No impact on existing tracks
- Can enable/disable services independently

### 4. **Maintainability**
- Clear mapping: service → track → events
- Self-documenting architecture
- Easier onboarding for new developers

## Event Flow

1. **Resource Creation**: User creates CVM/CLB in any region
2. **CloudAudit Capture**: Appropriate track captures the event
3. **COS Delivery**: Event delivered to shared COS bucket (2-6 min delay)
4. **SCF Trigger**: COS ObjectCreated event triggers function
5. **Event Processing**: Function reads event, determines type
6. **Tagging**: Function applies appropriate tags via Tag API
7. **Verification**: Tags visible in service console + Tag service

## Adding New Services

To add support for a new service (e.g., NAT Gateway):

### Step 1: Add Track Creation
In `ensure_audit_track_to_cos()`:
```python
# NAT Gateway track
nat_track_name = "tagger-nat-track"
nat_event_names = ["CreateNatGateway"]

# Check if exists, delete if needed
# ... (similar to CLB track logic) ...

# Create track
nat_creq = audit_models.CreateAuditTrackRequest()
setattr(nat_creq, "Name", nat_track_name)
setattr(nat_creq, "Status", 1)
setattr(nat_creq, "ActionType", "Write")
setattr(nat_creq, "ResourceType", "vpc")  # NAT uses vpc ResourceType
setattr(nat_creq, "EventNames", nat_event_names)
setattr(nat_creq, "Storage", storage)
# ... create and log ...
```

### Step 2: Add Event Handler
In `main_handler()`:
```python
# Handle NAT events
if event_name == "CreateNatGateway":
    if handle_nat_tagging(rec):
        tagged += 1
    continue
```

### Step 3: Implement Tagging Logic
```python
def handle_nat_tagging(rec: Dict[str, Any]) -> bool:
    """Handle NAT Gateway tagging."""
    event_name = rec.get("eventName", "")
    if event_name != "CreateNatGateway":
        return False
    
    # Extract NAT ID from resourceSet or responseElements
    nat_id = ...
    region = extract_region(rec)
    uin = extract_account_uin(rec)
    owner = get_owner(rec)
    
    # Build QCS
    qcs = f"qcs::vpc:{region}:uin/{uin}:natgateway/{nat_id}"
    
    # Tag resource
    tags = build_tags(owner)  # or build_nat_tags() for custom schema
    tag_resource_qcs(region, qcs, tags)
    
    return True
```

### Step 4: Update Documentation
- Add to README.md supported services list
- Update DEPLOYMENT.md with new track details
- Add to CHANGELOG.md

## Limitations & Constraints

### CloudAudit API Constraints
1. **No Wildcard + EventNames**: Cannot use `ResourceType: "*"` with specific events
2. **Required EventNames**: Cannot omit EventNames (empty list fails)
3. **Service-Specific ResourceType**: Must use exact service identifier (e.g., `"cvm"`, `"clb"`, `"vpc"`)

### Operational Constraints
1. **Track Limit**: Tencent Cloud may have maximum tracks per account (not documented)
2. **Delay**: 2-6 minute delay from event → COS → SCF trigger
3. **Eventual Consistency**: Tags may not appear immediately in all APIs

### CBS Limitations
CBS (Cloud Block Storage) has two separate issues:
1. **CloudAudit**: Event names (`CreateCbsStorages`, `AttachDisks`) not supported
2. **Tag API**: API accepts tags but CBS service doesn't honor them

Support ticket submitted to Tencent Cloud for clarification.

## Lessons Learned

1. **Test CloudAudit API Early**: CloudAudit has undocumented constraints
2. **Don't Assume Wildcard Support**: Many Tencent Cloud APIs don't support wildcards
3. **Separate Tracks are OK**: Multiple tracks with same COS destination works well
4. **Delete + Recreate is Easier**: ModifyAuditTrack has complex constraints, delete/create is simpler
5. **Service ResourceTypes Vary**: Some services use their service name (e.g., `clb`), others use category (e.g., NAT uses `vpc`)

## Future Considerations

### Potential Improvements
1. **Track Configuration File**: Move track definitions to JSON config
2. **Dynamic Track Discovery**: Auto-detect which services user wants to monitor
3. **Track Health Monitoring**: Alert if track stops delivering events
4. **Batch Track Operations**: Create all tracks in parallel for faster setup

### Service Expansion Candidates
High-cost resources to prioritize:
- **CDB (Cloud Database)**: `CreateDBInstance`, `CreateDBInstanceHour`
- **NAT Gateway**: `CreateNatGateway`
- **EIP (Elastic IP)**: `AllocateAddresses`
- **CFS (Cloud File Storage)**: `CreateCfsFileSystem`

---

**Last Updated**: 2026-02-22  
**Architecture Version**: 1.5.1  
**Status**: Production (CVM/CDH/CLB fully operational)
