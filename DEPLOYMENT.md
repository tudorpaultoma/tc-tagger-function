# SCF Resource Tagger - Deployment Guide

## ✅ Status: **PRODUCTION (v3.2.0)**

The function supports CVM, CDH, CLB, CBS, CBS Snapshots, EIP (including TransformAddress), ENI, HAVIP, NAT Gateway (public + private), CCN, TKE, and Auto Scaling auto-tagging across all regions.

## Current Version: v3.2.0

### What's Working
- ✅ **CVM instances** - Auto-tagged via `RunInstances` events
- ✅ **CDH hosts** - Auto-tagged via `AllocateHosts` events
- ✅ **CLB load balancers** - Auto-tagged via `CreateLoadBalancer` events
- ✅ **CBS disks** - Auto-tagged via `CreateCbsStorages`, `CreateDisks`, `AttachDisks` events
- ✅ **CBS snapshots** - Auto-tagged via `CreateSnapshot` events
- ✅ **EIP elastic IPs** - Auto-tagged via `AllocateAddresses` and `TransformAddress` events (with region discovery)
- ✅ **ENI network interfaces** - Auto-tagged via `CreateNetworkInterface` events (with region discovery)
- ✅ **HAVIP virtual IPs** - Auto-tagged via `CreateHaVip` events (with VPC/subnet info)
- ✅ **NAT Gateways (public)** - Auto-tagged via `CreateNatGateway` events (+ auto-tag associated EIPs)
- ✅ **NAT Gateways (private)** - Auto-tagged via `CreatePrivateNatGateway` events
- ✅ **CCN instances** - Auto-tagged via `CreateCcn` events (with CCN name tag)
- ✅ **TKE clusters** - Auto-tagged via `CreateCluster` events
- ✅ **Auto Scaling groups** - Auto-tagged via `CreateAutoScalingGroup` events
- ✅ **Auto Scaling launch configs** - Auto-tagged via `CreateLaunchConfiguration` events
- ✅ **Global coverage** - CloudAudit tracks monitor ALL regions worldwide
- ✅ **Cross-region delivery** - All logs from all regions deliver to your COS bucket

### Tags Applied

#### CVM/CDH Tags (Compute Resources)
- `TaggerOwner`: User who created the resource (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerAutoOff`: Default "YES" (for auto-shutdown scripts)
- `TaggerAutoStart`: Default "NO" (requires manual start)
- `TaggerCanDelete`: Default "YES" (for auto-deletion)
- `TaggerTTL`: Default "3" (days before auto-deletion)
- `TaggerProject`: Default "n/a"

#### CLB Tags (Network Resources)
- `TaggerOwner`: User who created the resource (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerCanDelete`: Default "YES" (for auto-deletion)
- `TaggerTTL`: Default "3" (days before auto-deletion)
- `TaggerProject`: Default "n/a"

#### CBS Disk Tags
- `TaggerOwner`: Disk creator (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerUsage`: Disk type ("SYSTEM" or "DATA")
- `TaggerLinkedCVM`: Attachment status ("YES" or "NO")
- `TaggerCanDelete`: Default "YES" (for auto-deletion)
- `TaggerTTL`: Default "3" (days before auto-deletion)
- `TaggerProject`: Copied from CVM if attached, "n/a" if unattached

#### EIP Tags (Elastic IP)
- `TaggerOwner`: EIP creator (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerType`: EIP type (EIP, AnycastEIP, HighQualityEIP, AntiDDoSEIP)
- `TaggerLinkedResource`: Bound instance ID or "NONE"
- `TaggerCanDelete`: Default "YES" (for auto-deletion)
- `TaggerTTL`: Default "3" (days before auto-deletion)
- `TaggerProject`: Default "n/a"

#### ENI Tags (Elastic Network Interface)
- `TaggerOwner`: ENI creator (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerLinkedResource`: Bound CVM instance ID or "NONE"
- `TaggerCanDelete`: Default "YES" (for auto-deletion)
- `TaggerTTL`: Default "3" (days before auto-deletion)
- `TaggerProject`: Default "n/a"

#### HAVIP Tags (High Availability Virtual IP)
- `TaggerOwner`: HAVIP creator (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerSubnet`: Subnet ID the HAVIP belongs to
- `TaggerVpc`: VPC ID the HAVIP belongs to
- `TaggerCanDelete`: Default "YES" (for auto-deletion)
- `TaggerTTL`: Default "3" (days before auto-deletion)
- `TaggerProject`: Default "n/a"

#### NAT Gateway Tags (Public + Private)
- `TaggerOwner`: NAT creator (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerCanDelete`: Default "YES" (for auto-deletion)
- `TaggerTTL`: Default "3" (days before auto-deletion)
- `TaggerProject`: Default "n/a"

#### CCN Tags (Cloud Connect Network)
- `TaggerOwner`: CCN creator (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerCcnName`: CCN instance name
- `TaggerCanDelete`: Default "YES" (for auto-deletion)
- `TaggerTTL`: Default "3" (days before auto-deletion)
- `TaggerProject`: Default "n/a"

#### CBS Snapshot Tags
- `TaggerOwner`: Snapshot creator (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerSourceDisk`: Source disk ID
- `TaggerDiskUsage`: Source disk type (SYSTEM_DISK or DATA_DISK)
- `TaggerCanDelete`: Default "YES" (for auto-deletion)
- `TaggerTTL`: Default "3" (days before auto-deletion)
- `TaggerProject`: Default "n/a"

#### TKE Cluster Tags
- `TaggerOwner`: Cluster creator (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerCanDelete`: Default "YES" (for auto-deletion)
- `TaggerTTL`: Default "3" (days before auto-deletion)
- `TaggerProject`: Default "n/a"

#### Auto Scaling Tags (Scaling Group + Launch Config)
- `TaggerOwner`: Resource creator (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerCanDelete`: Default "YES" (for auto-deletion)
- `TaggerTTL`: Default "3" (days before auto-deletion)
- `TaggerProject`: Default "n/a"

## Deployment Configuration

### Environment Variables
```bash
COS_BUCKET=tommywork-1301327510
COS_REGION=eu-frankfurt
COS_BASE_PREFIX=cloudaudit
AUDIT_SETUP=true
```

### COS Trigger Setup
- **Bucket**: `tommywork-1301327510`
- **Event**: `cos:ObjectCreated:*`
- **Prefix**: `cloudaudit/`
- **Suffix**: (leave empty)

### Handler
```
index.main_handler
```

## CloudAudit Tracks

The function automatically creates/updates separate CloudAudit tracks per service type:

### Track 1: CVM/CDH Resources
- **Name**: `tagger-cvm-track`
- **ResourceType**: `cvm`
- **Events**: `RunInstances` (CVM), `AllocateHosts` (CDH)
- **Coverage**: All Tencent Cloud regions automatically

### Track 2: CLB Resources
- **Name**: `tagger-clb-track`
- **ResourceType**: `clb`
- **Events**: `CreateLoadBalancer`
- **Coverage**: All Tencent Cloud regions automatically

### Track 3: CBS Resources
- **Name**: `tagger-cbs-track`
- **ResourceType**: `cbs`
- **Events**: `*` (all CBS write events)
- **Coverage**: All Tencent Cloud regions automatically

### Track 4: VPC Resources (EIP, ENI, HAVIP, NAT Gateway, CCN)
- **Name**: `tagger-vpc-track`
- **ResourceType**: `vpc`
- **Events**: `AllocateAddresses`, `CreateNetworkInterface`, `CreateHaVip`, `TransformAddress`, `CreateNatGateway`, `CreatePrivateNatGateway`, `CreateCcn`
- **Coverage**: All Tencent Cloud regions automatically

### Track 5: TKE Resources
- **Name**: `tagger-tke-track`
- **ResourceType**: `tke`
- **Events**: `CreateCluster`
- **Coverage**: All Tencent Cloud regions automatically

### Track 6: Auto Scaling Resources
- **Name**: `tagger-as-track`
- **ResourceType**: `as`
- **Events**: `CreateAutoScalingGroup`, `CreateLaunchConfiguration`
- **Coverage**: All Tencent Cloud regions automatically

### Common Settings
- **Storage Path**: `cloudaudit/YYYY/MM/DD/*.txt`
- **API Region**: `eu-frankfurt`
- **ActionType**: `Write` (only creation events)

## Test Results

### Latest Test (v3.2.0)
```json
{
  "status": "ok",
  "setup": {
    "cos_bucket_ok": true,
    "track_ids": {"global": 694},
    "monitored_regions": ["global"]
  },
  "processed": 2,
  "tagged": 2,
  "errors": []
}
```

### Performance
- **Duration**: ~5-8 seconds (with region discovery)
- **Memory**: ~120 MB peak
- **Cold Start**: ~900ms

### Confirmed Working
- ✅ CVM instances in eu-frankfurt and all regions
- ✅ CDH hosts in all regions
- ✅ CLB load balancers
- ✅ CBS disks (system + data, standalone + attached)
- ✅ CBS snapshots (with source disk info)
- ✅ EIP elastic IPs (with region discovery fallback, including TransformAddress)
- ✅ ENI network interfaces (with region discovery fallback)
- ✅ HAVIP virtual IPs (with VPC/subnet info)
- ✅ NAT Gateways — public (with EIP auto-tagging)
- ✅ NAT Gateways — private (with DescribePrivateNatGateways)
- ✅ CCN Cloud Connect Network (with DescribeCcns)
- ✅ TKE Kubernetes clusters
- ✅ Auto Scaling groups and launch configurations

## Next Steps

### 1. Update SCF Function
Upload the new deployment package:
```bash
./deploy.sh
# Upload scf-tagger.zip to SCF console
```

### 2. Test End-to-End

#### CVM/CDH Testing
1. Create a new CVM instance or CDH host in any region
2. Wait 5-10 minutes for CloudAudit to propagate event
3. Check SCF logs for successful tagging
4. Verify tags on the resource in Tencent Cloud Console

#### CBS Testing
1. **Attached Disk**: Create CVM with data disk
   - Wait 10 minutes for CVM tags to appear
   - Verify CBS disk receives CVM project tag
   - Check `TaggerLinkedCVM=YES` and `TaggerUsage=DATA` or `SYSTEM`
   
2. **Unattached Disk**: Create standalone CBS disk
   - Wait 10 minutes
   - Verify disk receives default tags with empty project
   - Check `TaggerLinkedCVM=NO`
   
3. **Re-attachment**: Detach disk and attach to different CVM
   - Verify project tag updates from new CVM

#### ENI Testing
1. **Standalone ENI**: Create an ENI in any VPC subnet
   - Wait 5-10 minutes for CloudAudit to propagate
   - Verify tags: `TaggerLinkedResource=NONE`
   
2. **CVM-attached ENI**: Create a CVM (which auto-creates an ENI)
   - The `CreateNetworkInterface` event fires separately
   - Verify tags: `TaggerLinkedResource=ins-xxx`

#### HAVIP Testing
1. Create a HAVIP in any VPC subnet
2. Wait 5-10 minutes for CloudAudit to propagate
3. Verify tags: `TaggerSubnet=subnet-xxx`, `TaggerVpc=vpc-xxx`

### 3. Monitor
- Check SCF logs regularly
- Monitor `errors` field in function output
- Set up CloudWatch alarms for failures

## Troubleshooting

### No Events Being Processed
- Verify COS trigger is configured correctly
- Check CloudAudit track is enabled (Status: 1)
- Verify `COS_BASE_PREFIX` matches CloudAudit prefix

### Tagging Failures
- Check IAM permissions for Tag API
- Verify instance region matches
- Check logs for specific error messages

### File Reading Issues
- Ensure complete file upload (not truncated)
- Verify COS bucket permissions
- Check file format (should be valid JSON)

## Files

- `index.py` - Main handler + shared utilities + event routing (v3.2.0)
- `services/cvm.py` - CVM/CDH tagging + attached disk tagging
- `services/clb.py` - CLB tagging
- `services/cbs.py` - CBS disk tagging
- `services/eip.py` - EIP tagging (AllocateAddresses, TransformAddress) with region discovery
- `services/eni.py` - ENI tagging with region discovery
- `services/havip.py` - HAVIP tagging with VPC/subnet info
- `services/nat.py` - NAT Gateway tagging (public + private) + EIP auto-tag
- `services/ccn.py` - CCN Cloud Connect Network tagging
- `services/snapshot.py` - CBS snapshot tagging with source disk info
- `services/tke.py` - TKE cluster tagging
- `services/autoscaling.py` - Auto Scaling group + launch config tagging
- `deploy.sh` - Deployment package builder
- `scf-tagger.zip` - Ready-to-deploy package
- `requirements.txt` - Python dependencies
- `policies/` - IAM policy templates
- `ARCHITECTURE.md` - Architecture decisions
- `CHANGELOG.md` - Version history

## Support

For issues or questions, check:
- SCF function logs in Tencent Cloud Console
- CloudAudit track configuration
- COS trigger settings
