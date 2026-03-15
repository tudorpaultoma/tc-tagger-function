# Architecture: Multi-Track CloudAudit Design

## Overview

This document explains the architectural decisions for the SCF Resource Tagger:
1. **Separate CloudAudit tracks per service type** instead of a single global track
2. **Modular service architecture** with each service in its own file

## File Structure

```
tc-tagger-function/
├── index.py                 # Main handler + shared utils + event routing
├── services/
│   ├── __init__.py          # Package init
│   ├── cvm.py               # CVM/CDH tagging (RunInstances, AllocateHosts)
│   ├── clb.py               # CLB tagging (CreateLoadBalancer)
│   ├── cbs.py               # CBS disk tagging (CreateCbsStorages, CreateDisks, AttachDisks)
│   ├── eip.py               # EIP tagging (AllocateAddresses, TransformAddress)
│   ├── eni.py               # ENI tagging (CreateNetworkInterface)
│   └── havip.py             # HAVIP tagging (CreateHaVip)
├── policies/
│   ├── audit-policy.json
│   ├── cos-policy.json
│   └── tag-policy.json
├── requirements.txt
├── ARCHITECTURE.md
├── CHANGELOG.md
└── ...
```

### Module Responsibilities

| File | Contains |
|------|----------|
| `index.py` | Shared utils (`make_tc_client`, `tag_resource_qcs`, `extract_region`, `get_owner`, etc.), COS reading, CloudAudit track setup, event routing |
| `services/cvm.py` | CVM/CDH tag builder, QCS extraction, CVM state polling, attached disk tagging |
| `services/clb.py` | CLB tag builder, LB ID extraction, CLB tagging handler |
| `services/cbs.py` | CBS tag builder, disk info queries, recent disk finder with retries, CBS tagging handler |
| `services/eip.py` | EIP tag builder, EIP info queries via VPC API, instance-based EIP discovery, region probing, EIP tagging handler |
| `services/eni.py` | ENI tag builder, ENI info queries via VPC API, ENI tagging handler |
| `services/havip.py` | HAVIP tag builder, HAVIP info queries via VPC API, HAVIP tagging handler |

Service modules import shared utils from `index` via `from index import make_tc_client, tag_resource_qcs, ...`.

## Background

### Initial Approach (Failed)
Initially attempted to use a single CloudAudit track with:
```json
{
  "ResourceType": "*",
  "EventNames": ["RunInstances", "AllocateHosts", "CreateLoadBalancer"]
}
```

**Result**: CloudAudit API rejected this with error:
```
[TencentCloudSDKException] code:InvalidParameter message:illegal params. EventNames error
```

### Root Cause
- CloudAudit **does not support** `ResourceType: "*"` (wildcard) when `EventNames` is specified
- `EventNames` is a **required parameter** (cannot be omitted)
- **Conclusion**: Wildcard + EventNames combination is fundamentally incompatible

## Current Architecture

### Multi-Track Approach

**Solution**: Create separate tracks for each service type with specific ResourceType.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                            CloudAudit Service                                │
│                                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │ Track 1: CVM │  │ Track 2: CLB │  │ Track 3: CBS │  │ Track 4: VPC │    │
│  │ RT: cvm      │  │ RT: clb      │  │ RT: cbs      │  │ RT: vpc      │    │
│  │ RunInstances │  │ CreateLoad.. │  │ ["*"]        │  │ AllocateAddr │    │
│  │ AllocateHost │  │              │  │              │  │ CreateNetwor │    │
│  │              │  │              │  │              │  │ CreateHaVip  │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │                 │                 │                 │              │
└─────────┼─────────────────┼─────────────────┼─────────────────┼─────────────┘
          │                 │                 │                 │
          └────────┬────────┴────────┬────────┴────────┬───────┘
                   │                 │                 │
                   ▼                 ▼                 ▼
              ┌──────────────────────────────────┐
              │         COS Bucket               │
              │       cloudaudit/                 │
              └───────────────┬──────────────────┘
                              │ COS Trigger
                              ▼
              ┌──────────────────────────────────┐
              │         SCF Function             │
              │       resource-tagger            │
              │                                  │
              │  index.py → event routing        │
              │    ├── services/cvm.py           │
              │    ├── services/clb.py           │
              │    ├── services/cbs.py           │
              │    ├── services/eip.py           │
              │    ├── services/eni.py           │
              │    └── services/havip.py         │
              └──────────────────────────────────┘
```

### Track Configuration

#### Track 1: CVM/CDH Resources
```json
{
  "Name": "tagger-cvm-track",
  "ResourceType": "cvm",
  "EventNames": ["RunInstances", "AllocateHosts"],
  "ActionType": "Write",
  "Storage": { "StorageType": "cos", "StorageName": "tommywork", "StorageRegion": "eu-frankfurt", "StoragePrefix": "cloudaudit" }
}
```

#### Track 2: CLB Resources
```json
{
  "Name": "tagger-clb-track",
  "ResourceType": "clb",
  "EventNames": ["CreateLoadBalancer"],
  "ActionType": "Write",
  "Storage": { "StorageType": "cos", "StorageName": "tommywork", "StorageRegion": "eu-frankfurt", "StoragePrefix": "cloudaudit" }
}
```

#### Track 3: CBS Resources
```json
{
  "Name": "tagger-cbs-track",
  "ResourceType": "cbs",
  "EventNames": ["*"],
  "ActionType": "Write",
  "Storage": { "StorageType": "cos", "StorageName": "tommywork", "StorageRegion": "eu-frankfurt", "StoragePrefix": "cloudaudit" }
}
```

#### Track 4: VPC Resources (EIP, ENI, HAVIP)
```json
{
  "Name": "tagger-vpc-track",
  "ResourceType": "vpc",
  "EventNames": ["AllocateAddresses", "CreateNetworkInterface", "CreateHaVip", "TransformAddress"],
  "ActionType": "Write",
  "Storage": { "StorageType": "cos", "StorageName": "tommywork", "StorageRegion": "eu-frankfurt", "StoragePrefix": "cloudaudit" }
}
```

> **Important**: The CloudAudit track uses `ResourceType: "vpc"` for EIP, ENI, and HAVIP, but the Tag API QCS differs per resource:
> - EIP: `qcs::cvm:...:eip/{id}` (CVM namespace — known Tencent Cloud inconsistency)
> - ENI: `qcs::vpc:...:eni/{id}` (VPC namespace)
> - HAVIP: `qcs::vpc:...:havip/{id}` (VPC namespace)

## Benefits

### 1. **CloudAudit API Compliance**
- Uses supported API patterns
- No "illegal params" errors
- Reliable event delivery

### 2. **Separation of Concerns**
- Each track has clear responsibility
- Each service in its own file — easier to debug
- Independent configuration per service

### 3. **Scalability**
- Easy to add new service types (new file + track entry + routing line)
- No impact on existing tracks or service modules
- Can enable/disable services independently

### 4. **Maintainability**
- Clear mapping: service → track → events → handler file
- Self-documenting architecture
- Easier onboarding for new developers

## Event Flow

1. **Resource Creation**: User creates CVM/CLB/CBS/EIP/ENI/HAVIP in any region
2. **CloudAudit Capture**: Appropriate track captures the event
3. **COS Delivery**: Event delivered to shared COS bucket (2-6 min delay)
4. **SCF Trigger**: COS ObjectCreated event triggers function
5. **Event Routing**: `index.py` reads event, routes to correct service handler
6. **Tagging**: Service handler builds tags + QCS, calls `tag_resource_qcs()`
7. **Verification**: Tags visible in service console + Tag service

## Adding New Services

To add support for a new service (e.g., NAT Gateway):

### Step 1: Create Service Module
Create `services/nat.py`:
```python
"""NAT Gateway Tagging Service"""
import json
import datetime
from typing import List, Dict, Any

def build_nat_tags(owner: str) -> List[Dict[str, str]]:
    today = datetime.date.today().isoformat()
    return [
        {"TagKey": "TaggerOwner",     "TagValue": owner or "unknown"},
        {"TagKey": "TaggerCreated",   "TagValue": today},
        {"TagKey": "TaggerCanDelete", "TagValue": "YES"},
        {"TagKey": "TaggerTTL",       "TagValue": "3"},
        {"TagKey": "TaggerProject",   "TagValue": "n/a"},
    ]

def handle_nat_tagging(rec: Dict[str, Any]) -> bool:
    from index import extract_region, extract_account_uin, get_owner, tag_resource_qcs
    
    if rec.get("eventName") != "CreateNatGateway":
        return False
    # Extract NAT ID, region, build QCS, apply tags...
    return True
```

### Step 2: Add Track Definition
In `index.py` `ensure_audit_track_to_cos()`, add to `tracks_config`:
```python
{"name": "tagger-nat-track", "resource_type": "nat", "event_names": ["CreateNatGateway"]},
```

### Step 3: Add Event Routing
In `index.py` `main_handler()`:
```python
from services.nat import handle_nat_tagging
# ...
if event_name == "CreateNatGateway":
    if handle_nat_tagging(rec):
        tagged += 1
    continue
```

### Step 4: Update Documentation
- Add to README.md supported services list
- Update this ARCHITECTURE.md
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
5. **Service ResourceTypes Vary**: Some services use their service name (e.g., `clb`), others use category (e.g., EIP uses `vpc` in CloudAudit but `cvm` in Tag API)
6. **Modular Split Works Well**: Service modules importing shared utils from `index` keeps things clean
7. **QCS Service Types ≠ CloudAudit ResourceTypes**: EIP uses `vpc` in CloudAudit but `cvm` in Tag API QCS — always verify with CloudAudit's `resources` field
8. **CloudAudit Region is Unreliable**: `eventRegion` and `eventSource` often point to the API gateway region, not the resource region — implement region discovery fallbacks
9. **resourceId Can Be a List**: CloudAudit delivers `resourceId` as stringified Python lists (`"['eip-xxx']"`) — always unwrap before use
10. **Tag API Fails Silently**: `TagResources` can return success with empty `FailedResources` even when the QCS service type is wrong — tags just don't appear
11. **VPC Track Consolidation**: EIP, ENI, and HAVIP all use `ResourceType: "vpc"` in CloudAudit — consolidating into one VPC track avoids hitting track limits

12. **COS Delivers JSON Strings**: `requestParameters` and `responseElements` in COS-delivered CloudAudit files are JSON strings, not parsed dicts — always deserialize before `.get()` access
13. **TransformAddress Masks Data**: CloudAudit masks `AddressId` as `***`, leaves `resourceSet` empty, and reports wrong region — use DescribeAddresses with instance-id filter across all regions to discover the EIP

### Potential Improvements
1. **Move shared utils to `common.py`**: When all services are modularized, extract shared utils from `index.py` to a dedicated module
2. **Track Configuration File**: Move track definitions to JSON config
3. **Dynamic Track Discovery**: Auto-detect which services user wants to monitor
4. **Track Health Monitoring**: Alert if track stops delivering events

### Service Expansion Candidates
High-cost resources to prioritize:
- **CDB (Cloud Database)**: `CreateDBInstance`, `CreateDBInstanceHour`
- **NAT Gateway**: `CreateNatGateway`
- **CFS (Cloud File Storage)**: `CreateCfsFileSystem`

---

**Last Updated**: 2026-03-15  
**Architecture Version**: 2.3.0  
**Status**: Production (CVM/CDH/CLB/CBS/EIP/ENI/HAVIP fully operational, including TransformAddress)
