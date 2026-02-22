# SCF Resource Tagger - Deployment Guide

## ✅ Status: **DEVELOPMENT (v1.5.0)**

The function supports CVM, CDH, and CLB tagging. CBS support is work in progress due to Tag API limitations.

## Current Version: v1.5.0

### What's Working
- ✅ **CVM instances** - Auto-tagged via `RunInstances` events
- ✅ **CDH hosts** - Auto-tagged via `AllocateHosts` events
- ✅ **CLB load balancers** - Auto-tagged via `CreateLoadBalancer` events (NEW)
- ✅ **Global coverage** - Single CloudAudit track monitors all regions
- ✅ **Cross-region delivery** - All logs deliver to Frankfurt COS bucket
- ⚠️ **CBS disks** - Function processes events but CBS doesn't honor Tag API (work in progress)

### Tags Applied

#### CVM/CDH Tags (Compute Resources)
- `TaggerOwner`: User who created the resource (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerAutoOff`: Default "YES" (for auto-shutdown scripts)
- `TaggerAutoStart`: Default "NO" (requires manual start)
- `TaggerTTL`: Default "7" (days before auto-deletion)
- `TaggerProject`: Default "n/a"

#### CLB Tags (Network Resources)
- `TaggerOwner`: User who created the resource (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerTTL`: Default "7" (days before auto-deletion)
- `TaggerDelete`: Default "YES" (for auto-deletion)
- `TaggerProject`: Default "n/a"

**Note**: CLB uses `TaggerDelete` instead of `TaggerAutoOff`/`TaggerAutoStart` since load balancers can only be created or deleted (no start/stop operations).

#### CBS Disk Tags ⚠️
- `TaggerOwner`: Disk creator (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerTTL`: Default "7" (days before auto-deletion)
- `TaggerProject`: Copied from CVM if attached, empty string if unattached
- `TaggerUsage`: Disk type ("SYSTEM" or "DATA")
- `TaggerLinkedCVM`: Attachment status ("YES" or "NO")

**Note**: CBS tags are applied successfully via Tag API but do not appear in CBS console/API. Support ticket submitted.

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
- **Type**: COS delivery
- **ResourceType**: `cvm`
- **Events**: `RunInstances` (CVM), `AllocateHosts` (CDH)
- **Coverage**: All Tencent Cloud regions automatically

### Track 2: CLB Resources
- **Name**: `tagger-clb-track`
- **Type**: COS delivery
- **ResourceType**: `clb`
- **Events**: `CreateLoadBalancer`
- **Coverage**: All Tencent Cloud regions automatically

### Common Settings
- **Storage Path**: `cloudaudit/YYYY/MM/DD/*.txt`
- **API Region**: `ap-guangzhou` (Tencent Cloud requirement)
- **ActionType**: `Write` (only creation events)

**Important**: CloudAudit requires separate tracks per service type. Using `ResourceType: "*"` (wildcard) is not supported when specifying EventNames.

**Note**: CloudAudit tracks are global by design - one track monitors all regions.

## Test Results

### Latest Test (v1.4.0)
Awaiting testing after CBS implementation.

### Previous Test (v1.3.0)
```json
{
  "status": "ok",
  "setup": {
    "cos_bucket_ok": true,
    "track_ids": {"global": 641},
    "monitored_regions": ["global"]
  },
  "processed": 0,
  "tagged": 0,
  "errors": []
}
```

### Performance
- **Duration**: ~2-4 seconds
- **Memory**: ~27 MB
- **Cold Start**: ~800-850ms

### Confirmed Working
- ✅ CVM instances in eu-frankfurt
- ✅ CVM instances in ap-shanghai
- ✅ CDH hosts in all regions
- ✅ CloudAudit track creation (Track ID: 641)
- ⏳ CBS disks (pending testing)

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

- `index.py` - Main function code (v1.4.0)
- `deploy.sh` - Deployment package builder
- `scf-tagger.zip` - Ready-to-deploy package
- `requirements.txt` - Python dependencies
- `policies/` - IAM policy templates
- `CHANGELOG.md` - Version history

## Support

For issues or questions, check:
- SCF function logs in Tencent Cloud Console
- CloudAudit track configuration
- COS trigger settings
