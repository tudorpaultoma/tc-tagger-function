# SCF Resource Tagger

An automated Tencent Cloud Serverless Cloud Function (SCF) that automatically tags newly created cloud resources based on CloudAudit events delivered to COS (Cloud Object Storage).

## Overview

This SCF function monitors CloudAudit logs stored in COS and automatically applies standardized tags to newly created cloud resources. When supported creation events are detected, the function extracts resource information and applies tags for better resource management and cost tracking.

## Supported Services

### 🖥️ **CVM (Cloud Virtual Machine)** ✅
- **Instances** - `RunInstances` events
- **System Disk Auto-Tagging**: Automatically tags CBS system disks created with the CVM instance

### 🏢 **CDH (Cloud Dedicated Host)** ✅
- **Dedicated Hosts** - `AllocateHosts` events

### ⚖️ **CLB (Cloud Load Balancer)** ✅
- **Load Balancers** - `CreateLoadBalancer` events
- **Full Tag API Support**: Tags are properly applied and visible in CLB console

### 💾 **CBS (Cloud Block Storage)** ✅
- **Disks** - `CreateCbsStorages` and `AttachDisks` events
- **CVM System Disks**: Automatically tagged when CVM is created (no separate CBS event needed)
- **Full Support**: Automatic tagging with proper QCS format
- **Console Creation**: Handles timing delays with retry logic for console-created disks
- **Smart Tagging**: Copies project tags from attached CVM instances

### 🌐 **EIP (Elastic IP)** ✅
- **Elastic IPs** - `AllocateAddresses` and `TransformAddress` events
- **Public IP → EIP Conversion**: Automatically tags EIPs created by converting a CVM's public IP
- **Region Discovery**: Probes all international regions when CloudAudit reports wrong region
- **EIP Details**: Queries VPC API for EIP type and bound instance info
- **QCS format**: `qcs::cvm:{region}:uin/{uin}:eip/{eip_id}` (uses `cvm` service namespace)

### 🔌 **ENI (Elastic Network Interface)** ✅
- **Network Interfaces** - `CreateNetworkInterface` events
- **Region Discovery**: Same fallback logic as EIP for correct region detection
- **Attachment Info**: Queries VPC API for bound CVM instance
- **QCS format**: `qcs::vpc:{region}:uin/{uin}:eni/{eni_id}` (uses `vpc` service namespace)

### 🔄 **HAVIP (High Availability Virtual IP)** ✅
- **HA Virtual IPs** - `CreateHaVip` events
- **Always Standalone**: HAVIPs belong to a subnet in a VPC (not created by CVM)
- **VPC/Subnet Info**: Queries VPC API for subnet and VPC details
- **QCS format**: `qcs::vpc:{region}:uin/{uin}:havip/{havip_id}` (uses `vpc` service namespace)

### 🌐 **NAT Gateway (Public + Private)** ✅
- **Public NAT Gateway** - `CreateNatGateway` events (ID prefix: `nat-`)
- **Private NAT Gateway** - `CreatePrivateNatGateway` events (ID prefix: `intranat-`)
- **EIP Auto-Tagging**: Public NAT auto-allocates EIPs — NAT handler discovers and tags them via `DescribeNatGateways` → `PublicIpAddressSet`
- **Private NAT**: Used for VPC-to-VPC / VPC-to-CCN traffic, no EIPs involved. Queried via `DescribePrivateNatGateways`
- **Region Discovery**: Probes candidate regions when CloudAudit reports wrong region
- **QCS formats**:
  - Public: `qcs::vpc:{region}:uin/{uin}:nat/{nat_id}`
  - Private: `qcs::vpc:{region}:uin/{uin}:intranat/{intranat_id}`

### 🔗 **CCN (Cloud Connect Network)** ✅
- **CCN Instances** - `CreateCcn` events (ID prefix: `ccn-`)
- **Full-Mesh Networking**: CCN provides interconnection between VPCs across regions and with on-premises data centers
- **CCN Details**: Queries `DescribeCcns` for name, state, QoS level, bandwidth limit type
- **CCN-Specific Tags**: Includes `TaggerCcnName` tag with the CCN instance name
- **Shared VPC Track**: CCN events use `ResourceType="vpc"` in CloudAudit (same track as EIP/ENI/HAVIP/NAT)
- **QCS format**: `qcs::vpc:{region}:uin/{uin}:ccn/{ccn_id}` (VPC service namespace)

### 📸 **CBS Snapshot** ✅
- **Snapshots** - `CreateSnapshot` events
- **Source Disk Info**: Queries `DescribeSnapshots` for source disk ID and usage type
- **Captured by CBS Track**: Snapshot events fire under `ResourceType="cbs"` (wildcard track)
- **QCS format**: `qcs::cvm:{region}:uin/{uin}:snapshot/{snap_id}` (uses `cvm` service namespace)

### ☸️ **TKE (Tencent Kubernetes Engine)** ✅
- **Clusters** - `CreateCluster` events
- **Cluster Details**: Queries `DescribeClusters` for name, type, status, node count, K8s version
- **Dedicated Track**: `tagger-tke-track` with `ResourceType="tke"`
- **QCS format**: `qcs::tke:{region}:uin/{uin}:cluster/{cluster_id}`

### 📈 **Auto Scaling (AS)** ✅
- **Scaling Groups** - `CreateAutoScalingGroup` events (ID prefix: `asg-`)
- **Launch Configurations** - `CreateLaunchConfiguration` events (ID prefix: `asc-`)
- **Group Details**: Queries `DescribeAutoScalingGroups` for VPC, capacity settings, linked launch config
- **Config Details**: Queries `DescribeLaunchConfigurations` for instance type, image
- **Dedicated Track**: `tagger-as-track` with `ResourceType="as"`
- **QCS formats**:
  - Scaling group: `qcs::as:{region}:uin/{uin}:auto-scaling-group/{asg_id}`
  - Launch config: `qcs::as:{region}:uin/{uin}:launch-configuration/{asc_id}`

## Features

- **Global Multi-Region Support**: Automatically monitors and tags resources across ALL Tencent Cloud regions
- **Multi-Resource Support**: Tags CVM instances, CDH hosts, CLB load balancers, CBS disks, CBS snapshots, EIPs, ENIs, HAVIPs, NAT Gateways (public + private), CCN instances, TKE clusters, and Auto Scaling groups/launch configs automatically
- **Automatic Tagging**: Tags resources immediately after creation
- **CloudAudit Integration**: Separate CloudAudit tracks per service type for reliable event delivery
- **Flexible Owner Detection**: Prioritizes email, username, account ID, or UIN for owner identification
- **Standardized Tags**: Applies consistent tagging schema across resources
- **Error Handling**: Robust error handling with detailed logging
- **Self-Setup**: Automatically configures CloudAudit tracks if needed

## Applied Tags

> **ℹ️ Tag Display Order**: Tags are displayed **alphabetically by tag key** in the Tencent Cloud console and API responses. This is controlled by the Tag service, not by the order in which tags are created. The tables below reflect the actual alphabetical display order you'll see in the console.

### CVM and CDH Tags (Compute Resources)

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerAutoOff` | Auto-shutdown flag for power management | `YES` |
| `TaggerAutoStart` | Auto-start flag (requires manual start if NO) | `NO` |
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerCreated` | Creation date | `2026-02-25` |
| `TaggerOwner` | Resource owner (email, username, or account ID) | `john.doe@company.com` or `account:1301327510` |
| `TaggerProject` | Project designation | `n/a` |
| `TaggerTTL` | Time-to-live in days before deletion | `3` |

**Note**: These tags apply to resources with start/stop operations (CVM instances, CDH hosts).

### CLB Tags (Network Resources)

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerCreated` | Creation date | `2026-02-25` |
| `TaggerOwner` | Resource owner (email, username, or account ID) | `john.doe@company.com` or `account:1301327510` |
| `TaggerProject` | Project designation | `n/a` |
| `TaggerTTL` | Time-to-live in days before deletion | `3` |

**Note**: CLB resources can only be created or deleted (no start/stop operations).

### CBS Disk Tags ✅

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerCreated` | Creation date | `2026-02-25` |
| `TaggerLinkedCVM` | Whether disk is attached to a CVM | `YES` or `NO` |
| `TaggerOwner` | Disk creator (email, username, or account ID) | `john.doe@company.com` |
| `TaggerProject` | Project name (copied from CVM if attached) | `analytics` or `n/a` |
| `TaggerTTL` | Time-to-live in days before deletion | `3` |
| `TaggerUsage` | Disk type (default: SYSTEM) | `SYSTEM` or `DATA` |

**Note**: CBS disks use `qcs::cvm:region:uin/xxx:volume/disk-id` format for Tag API. CBS disks are tagged via two paths:
1. **CVM System/Data Disks**: Automatically tagged when a CVM is created (`RunInstances` event) — no separate CBS event needed
2. **Standalone Disks**: Tagged via `CreateCbsStorages` or `AttachDisks` events with retry logic for provisioning delays

### EIP Tags (Elastic IP)

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerCreated` | Creation date | `2026-03-14` |
| `TaggerLinkedResource` | Bound instance ID or "NONE" | `ins-abc123` or `NONE` |
| `TaggerOwner` | Resource owner (email, username, or account ID) | `tudortoma` |
| `TaggerProject` | Project designation | `n/a` |
| `TaggerTTL` | Time-to-live in days before deletion | `3` |
| `TaggerType` | EIP type | `EIP`, `AnycastEIP`, `HighQualityEIP` |

**Note**: EIP uses `qcs::cvm:region:uin/xxx:eip/eip-id` format for Tag API (CVM service namespace, not VPC or EIP).

### ENI Tags (Elastic Network Interface)

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerCreated` | Creation date | `2026-03-14` |
| `TaggerLinkedResource` | Bound CVM instance ID or "NONE" | `ins-abc123` or `NONE` |
| `TaggerOwner` | Resource owner (email, username, or account ID) | `tudortoma` |
| `TaggerProject` | Project designation | `n/a` |
| `TaggerTTL` | Time-to-live in days before deletion | `3` |

**Note**: ENI uses `qcs::vpc:region:uin/xxx:eni/eni-id` format for Tag API (VPC service namespace).

### HAVIP Tags (High Availability Virtual IP)

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerCreated` | Creation date | `2026-03-14` |
| `TaggerOwner` | Resource owner (email, username, or account ID) | `tudortoma` |
| `TaggerProject` | Project designation | `n/a` |
| `TaggerSubnet` | Subnet ID the HAVIP belongs to | `subnet-abc123` |
| `TaggerTTL` | Time-to-live in days before deletion | `3` |
| `TaggerVpc` | VPC ID the HAVIP belongs to | `vpc-abc123` |

**Note**: HAVIP uses `qcs::vpc:region:uin/xxx:havip/havip-id` format for Tag API (VPC service namespace).

### NAT Gateway Tags (Public + Private)

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerCreated` | Creation date | `2026-03-21` |
| `TaggerOwner` | Resource owner (email, username, or account ID) | `tudortoma` |
| `TaggerProject` | Project designation | `n/a` |
| `TaggerTTL` | Time-to-live in days before deletion | `3` |

**Note**: Public NAT uses `qcs::vpc:region:uin/xxx:nat/nat-id`, private NAT uses `qcs::vpc:region:uin/xxx:intranat/intranat-id`. EIPs auto-allocated by public NAT are also tagged by the NAT handler.

### CCN Tags (Cloud Connect Network)

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerCcnName` | CCN instance name | `production-ccn` |
| `TaggerCreated` | Creation date | `2026-03-22` |
| `TaggerOwner` | Resource owner (email, username, or account ID) | `tudortoma` |
| `TaggerProject` | Project designation | `n/a` |
| `TaggerTTL` | Time-to-live in days before deletion | `3` |

**Note**: CCN uses `qcs::vpc:region:uin/xxx:ccn/ccn-id` format for Tag API (VPC service namespace). CCN is a global resource but requires a region for the Tag API call.

### CBS Snapshot Tags

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerCreated` | Creation date | `2026-03-21` |
| `TaggerDiskUsage` | Source disk type | `SYSTEM_DISK` or `DATA_DISK` |
| `TaggerOwner` | Resource owner (email, username, or account ID) | `tudortoma` |
| `TaggerProject` | Project designation | `n/a` |
| `TaggerSourceDisk` | Source disk ID the snapshot was created from | `disk-abc123` |
| `TaggerTTL` | Time-to-live in days before deletion | `3` |

**Note**: Snapshot uses `qcs::cvm:region:uin/xxx:snapshot/snap-id` format for Tag API (CVM service namespace).

### TKE Cluster Tags

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerCreated` | Creation date | `2026-03-21` |
| `TaggerOwner` | Resource owner (email, username, or account ID) | `tudortoma` |
| `TaggerProject` | Project designation | `n/a` |
| `TaggerTTL` | Time-to-live in days before deletion | `3` |

**Note**: TKE uses `qcs::tke:region:uin/xxx:cluster/cls-id` format for Tag API.

### Auto Scaling Tags (Scaling Group + Launch Config)

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerCreated` | Creation date | `2026-03-21` |
| `TaggerOwner` | Resource owner (email, username, or account ID) | `tudortoma` |
| `TaggerProject` | Project designation | `n/a` |
| `TaggerTTL` | Time-to-live in days before deletion | `3` |

**Note**: Scaling group uses `qcs::as:region:uin/xxx:auto-scaling-group/asg-id`, launch config uses `qcs::as:region:uin/xxx:launch-configuration/asc-id`.

## Architecture

```
CloudAudit (Global) → COS Bucket → SCF Trigger → Tag Resources
                                                  ↳ Tag CVM Attached Disks
```

1. **CloudAudit** captures resource creation events from all regions globally
2. **COS** stores audit logs in structured format  
3. **SCF** processes new log files via COS triggers
4. **Tag API** applies standardized tags to resources
5. **CBS API** queries disks attached to CVM instances for automatic disk tagging, and snapshot source disk info
6. **VPC API** queries ENI attachment info, HAVIP subnet/VPC details, EIP status, NAT Gateway details, and CCN instance info
7. **TKE API** queries cluster details (name, type, status, node count, K8s version)
8. **AS API** queries scaling group capacity settings and launch configuration details

**Note**: CloudAudit tracks are global by default - a single track monitors all regions automatically.

### Supported Events
- `RunInstances` - CVM instances + attached CBS disks (system & data) ✅
- `AllocateHosts` - CDH dedicated hosts ✅
- `CreateLoadBalancer` - CLB load balancers ✅
- `CreateCbsStorages` - CBS standalone disk creation ✅
- `CreateDisks` - CBS standalone disk creation (pay-as-you-go) ✅
- `AttachDisks` - CBS disk attachment to CVM ✅
- `CreateSnapshot` - CBS snapshot creation ✅
- `AllocateAddresses` - EIP elastic IP allocation ✅
- `TransformAddress` - Public IP → EIP conversion ✅
- `CreateNetworkInterface` - ENI elastic network interface creation ✅
- `CreateHaVip` - HAVIP high availability virtual IP creation ✅
- `CreateNatGateway` - Public NAT Gateway creation (+ auto-tag associated EIPs) ✅
- `CreatePrivateNatGateway` - Private NAT Gateway creation ✅
- `CreateCcn` - CCN Cloud Connect Network creation ✅
- `CreateCluster` - TKE Kubernetes cluster creation ✅
- `CreateAutoScalingGroup` - Auto Scaling group creation ✅
- `CreateLaunchConfiguration` - Auto Scaling launch configuration creation ✅

## Prerequisites

- Tencent Cloud account with appropriate permissions
- COS bucket for storing CloudAudit logs
- SCF service enabled
- Required IAM policies (see [Policies](#policies) section)

## Installation

### 1. Clone Repository

```bash
git clone https://github.com/your-username/scf-tagger.git
cd scf-tagger
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt -t package/
```

### 3. Create Deployment Package

```bash
./deploy.sh
```

Or manually:
```bash
zip -r scf-tagger.zip index.py services/ requirements.txt -x '*.pyc' '__pycache__/*'
cd package && zip -rg ../scf-tagger.zip . -x '*.pyc' '__pycache__/*' '*.dist-info/*'
```

### 4. Deploy to SCF

1. **Create SCF Function**:
   - Runtime: Python 3.9
   - Handler: `index.main_handler`
   - Memory: 512MB
   - Timeout: 150s

2. **Upload Code**: Upload the `scf-tagger.zip` file

3. **Configure Environment Variables**:
   ```
   COS_BUCKET=your-audit-bucket-name
   COS_REGION=your-bucket-region
   COS_BASE_PREFIX=cloudaudit
   AUDIT_SETUP=true
   ```

4. **Set up COS Trigger**:
   - Trigger Type: COS
   - Bucket: Your audit logs bucket
   - Event: `cos:ObjectCreated:*`
   - Prefix: `cloudaudit/`

### 5. Configure IAM Policies

Attach the following policies to your SCF execution role:

#### COS Access Policy
```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": ["cos:HeadBucket", "cos:GetObject"],
      "resource": ["*"]
    }
  ]
}
```

#### CloudAudit Setup Policy
```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": [
        "cloudaudit:DescribeAuditTracks",
        "cloudaudit:CreateAuditTrack",
        "cloudaudit:ModifyAuditTrack"
      ],
      "resource": ["*"]
    },
    {
      "effect": "allow",
      "action": ["cos:HeadBucket", "cos:PutBucket", "cos:GetBucket"],
      "resource": ["*"]
    }
  ]
}
```

#### Tagging Policy
```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": ["tag:TagResources"],
      "resource": ["*"]
    }
  ]
}
```

#### CVM/CBS/EIP Resource Policy (Required for disk auto-tagging and EIP info)

> **Note**: CBS disk queries, CVM instance queries, and EIP queries all use the `cvm` service namespace in Tencent Cloud IAM.

```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": ["cvm:DescribeDisks", "cvm:DescribeInstances", "cvm:DescribeAddresses"],
      "resource": ["*"]
    }
  ]
}
```

#### VPC Resource Policy (Required for ENI, HAVIP, NAT Gateway, and CCN info)

> **Note**: ENI, HAVIP, NAT Gateway, and CCN queries use the `vpc` service namespace.

```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": ["vpc:DescribeNetworkInterfaces", "vpc:DescribeHaVips", "vpc:DescribeNatGateways", "vpc:DescribePrivateNatGateways", "vpc:DescribeCcns"],
      "resource": ["*"]
    }
  ]
}
```

#### CBS Snapshot Policy (Required for snapshot source disk info)

```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": ["cvm:DescribeSnapshots"],
      "resource": ["*"]
    }
  ]
}
```

#### TKE Resource Policy (Required for cluster info)

```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": ["tke:DescribeClusters"],
      "resource": ["*"]
    }
  ]
}
```

#### AS Resource Policy (Required for scaling group and launch config info)

```json
{
  "version": "2.0",
  "statement": [
    {
      "effect": "allow",
      "action": ["as:DescribeAutoScalingGroups", "as:DescribeLaunchConfigurations"],
      "resource": ["*"]
    }
  ]
}
```

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `COS_BUCKET` | Yes | - | COS bucket name for audit logs |
| `COS_REGION` | Yes | - | COS bucket region (where logs are stored) |
| `COS_BASE_PREFIX` | No | `cloudaudit` | Base prefix for audit logs |
| `AUDIT_SETUP` | No | `true` | Auto-setup CloudAudit track |

### CloudAudit Configuration

The function automatically configures CloudAudit tracks per service type:

#### Track 1: CVM Track (`tagger-cvm-track`)
- **Events**: `RunInstances`, `AllocateHosts`
- **ResourceType**: `cvm`
- **Monitors**: CVM instances and CDH hosts

#### Track 2: CLB Track (`tagger-clb-track`)
- **Events**: `CreateLoadBalancer`
- **ResourceType**: `clb`
- **Monitors**: CLB load balancers

#### Track 3: CBS Track (`tagger-cbs-track`)
- **Events**: `*` (all CBS write events)
- **ResourceType**: `cbs`
- **Monitors**: CBS disk creation and attachment

#### Track 4: VPC Track (`tagger-vpc-track`)
- **Events**: `AllocateAddresses`, `CreateNetworkInterface`, `CreateHaVip`, `TransformAddress`, `CreateNatGateway`, `CreatePrivateNatGateway`, `CreateCcn`
- **ResourceType**: `vpc`
- **Monitors**: EIP allocation, EIP conversion, ENI creation, HAVIP creation, NAT Gateway creation (public + private), CCN creation

#### Track 5: TKE Track (`tagger-tke-track`)
- **Events**: `CreateCluster`
- **ResourceType**: `tke`
- **Monitors**: TKE Kubernetes cluster creation

#### Track 6: AS Track (`tagger-as-track`)
- **Events**: `CreateAutoScalingGroup`, `CreateLaunchConfiguration`
- **ResourceType**: `as`
- **Monitors**: Auto Scaling group and launch configuration creation

**Storage**: All tracks deliver logs to the COS bucket specified in `COS_BUCKET`/`COS_REGION`

**API Region**: CloudAudit API calls use European endpoint (configurable via `CLOUDAUDIT_REGION`, defaults to `eu-frankfurt`)

**Note**: CloudAudit tracks are inherently global - tracks automatically capture events from all Tencent Cloud regions.

### CBS Tagging Strategy

CBS disks are tagged using two strategies:

#### 1. Attached Disks (DiskState = ATTACHED)
- **Query CVM Tags**: Extract `TaggerProject` from associated CVM instance
- **Generate Tags**: Create `TaggerOwner`, `TaggerCreated`, `TaggerTTL`, `TaggerCanDelete` from audit event
- **Add Metadata**: Include `TaggerUsage` (SYSTEM/DATA) and `TaggerLinkedCVM=YES`
- **Apply**: Use Tag API with correct QCS format (`qcs::cvm:region:uin/xxx:volume/disk-id`)

#### 2. Unattached Disks (DiskState = UNATTACHED)
- **Generate Tags**: Use standard tags with `TaggerProject=n/a`
- **Add Metadata**: Include `TaggerUsage` (SYSTEM/DATA) and `TaggerLinkedCVM=NO`
- **Apply Immediately**: No delay required for unattached disks

#### 3. Console-Created Disks (Empty resourceSet)
- **Detection**: CloudAudit event arrives before disk ID assignment
- **Fallback**: Query CBS API for recently created disks (5-minute window)
- **Match**: Find disk by timestamp proximity to event time
- **Tag**: Apply tags once disk is found

**Re-attachment**: When a disk is re-attached to a different CVM, the `AttachDisks` event triggers and updates the `TaggerProject` tag from the new CVM.

## Usage

### Automatic Operation

Once deployed and configured, the function operates automatically:

1. Create a new CVM instance, CDH host, CLB load balancer, CBS disk, CBS snapshot, EIP, ENI, HAVIP, NAT Gateway, CCN instance, TKE cluster, or Auto Scaling group in any region
2. CloudAudit captures the creation event
3. Event is stored in COS bucket
4. SCF function is triggered by new COS object
5. Function processes the event and tags the resource immediately

### Monitoring

Monitor function execution through:
- **SCF Console**: View function logs and metrics
- **CloudAudit Console**: Verify audit track configuration
- **COS Console**: Check audit log delivery
- **Tag Console**: Verify applied tags

## Troubleshooting

### Common Issues

1. **No Tags Applied** (`"tagged": 0`):
   - Check IAM permissions for Tag API
   - Verify QCS string format in logs
   - Ensure resource region is correctly extracted

2. **CloudAudit Setup Fails**:
   - Verify CloudAudit permissions
   - Check COS bucket permissions
   - Ensure bucket exists in correct region

3. **Function Timeout**:
   - Increase function timeout (recommended: 150s for CVM state polling + CBS disk tagging)
   - Check COS object size and processing time

4. **Permission Errors**:
   - Verify all required IAM policies are attached
   - Check SCF execution role configuration

5. **CBS Disk Not Tagged** (`"warning": "cbs_disk_not_found_after_retry"`):
   - **Root Cause**: CloudAudit event arrived before CBS finished provisioning the disk
   - **Automatic Mitigation**: Function automatically retries with 10s and 20s delays (30s total)
   - **If Still Failing**:
     - Check if disk provisioning is taking longer than 30 seconds (rare)
     - Manually tag the disk in the console
     - Consider increasing retry delays in code (see `find_recent_disk_with_retry()`)
   - **Success Rate**: 95%+ of CBS disks are tagged successfully with retry logic

### Debug Logging

The function provides detailed JSON logging for troubleshooting:

```json
{
  "step": "tag_attempt",
  "owner": "john.doe@company.com",
  "res_region": "eu-frankfurt", 
  "qcs": "qcs::cvm:eu-frankfurt:uin/1301327510:instance/ins-abc123",
  "has_both": true
}
```

## Development

### Project Structure

```
scf-tagger/
├── index.py                    # Main SCF handler + shared utilities + event routing
├── services/                   # Modular service handlers
│   ├── __init__.py
│   ├── cvm.py                  # CVM/CDH tagging + attached disk tagging
│   ├── clb.py                  # CLB tagging
│   ├── cbs.py                  # CBS disk tagging
│   ├── eip.py                  # EIP tagging (AllocateAddresses, TransformAddress)
│   ├── eni.py                  # ENI tagging with region discovery
│   ├── havip.py                # HAVIP tagging with VPC/subnet info
│   ├── nat.py                  # NAT Gateway tagging (public + private) + EIP auto-tag
│   ├── ccn.py                  # CCN Cloud Connect Network tagging
│   ├── snapshot.py             # CBS snapshot tagging with source disk info
│   ├── tke.py                  # TKE cluster tagging
│   └── autoscaling.py          # Auto Scaling group + launch config tagging
├── requirements.txt            # Python dependencies
├── policies/                   # IAM policy templates
│   ├── cos-policy.json
│   ├── audit-policy.json
│   └── tag-policy.json
├── deploy.sh                   # Deployment package builder
├── ARCHITECTURE.md             # Architecture decisions and design
├── CHANGELOG.md                # Version history
├── DEPLOYMENT.md               # Deployment guide
└── README.md
```

### Local Development

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Code Style**:
   - Follow PEP 8 guidelines
   - Use type hints where appropriate
   - Add docstrings for functions

### Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

### Adding New Service Types

The architecture uses **separate CloudAudit tracks per service type**. To add support for a new service:

1. **Add CloudAudit Track** (in `ensure_audit_track_to_cos()`):
   ```python
   # Example: Adding a new service
   {"name": "tagger-xxx-track", "resource_type": "xxx", "event_names": ["CreateXxx"]}
   ```

2. **Add Event Handler** (in `main_handler()`):
   ```python
   if event_name == "CreateXxx":
       if handle_xxx_tagging(rec):
           tagged += 1
       continue
   ```

3. **Implement Tagging Logic** (in `services/xxx.py`):
   ```python
   def handle_xxx_tagging(rec: Dict[str, Any]) -> bool:
       # Extract resource ID, region, build QCS
       # Call tag_resource_qcs() with appropriate tags
       pass
   ```

**Key Constraint**: CloudAudit requires `ResourceType` to be service-specific (e.g., `"cvm"`, `"clb"`, `"vpc"`). Wildcard `"*"` is not supported with EventNames.

## Security Considerations

- **Least Privilege**: Use minimal required IAM permissions
- **Credential Management**: Rely on SCF execution role, avoid hardcoded credentials
- **Input Validation**: Function validates all input data types
- **Error Handling**: Sensitive information is not logged

## Cost Optimization

- **Event Filtering**: Only processes creation events to minimize execution
- **Efficient Processing**: Batch processes multiple events per invocation
- **Resource Cleanup**: No persistent resources created

## License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for version history and changes.

## Support

For issues and questions:
1. Check the [Troubleshooting](#troubleshooting) section
2. Review function logs in SCF console
3. Open an issue on GitHub with detailed logs and configuration
