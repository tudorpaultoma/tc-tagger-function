# SCF Resource Tagger

An automated Tencent Cloud Serverless Cloud Function (SCF) that automatically tags newly created cloud resources based on CloudAudit events delivered to COS (Cloud Object Storage).

## Overview

This SCF function monitors CloudAudit logs stored in COS and automatically applies standardized tags to newly created cloud resources. When supported creation events are detected, the function extracts resource information and applies tags for better resource management and cost tracking.

## Supported Services

### 🖥️ **CVM (Cloud Virtual Machine)** ✅
- **Instances** - `RunInstances` events

### 🏢 **CDH (Cloud Dedicated Host)** ✅
- **Dedicated Hosts** - `AllocateHosts` events

### ⚖️ **CLB (Cloud Load Balancer)** ✅
- **Load Balancers** - `CreateLoadBalancer` events
- **Full Tag API Support**: Tags are properly applied and visible in CLB console

### 💾 **CBS (Cloud Block Storage)** ✅
- **Disks** - `CreateCbsStorages` and `AttachDisks` events
- **Full Support**: Automatic tagging with proper QCS format
- **Console Creation**: Handles timing delays for console-created disks
- **Smart Tagging**: Copies project tags from attached CVM instances

## Features

- **Global Multi-Region Support**: Automatically monitors and tags resources across ALL Tencent Cloud regions
- **Multi-Resource Support**: Tags CVM instances, CDH hosts, CLB load balancers, and CBS disks automatically
- **Automatic Tagging**: Tags resources immediately after creation
- **CloudAudit Integration**: Separate CloudAudit tracks per service type for reliable event delivery
- **Flexible Owner Detection**: Prioritizes email, username, account ID, or UIN for owner identification
- **Standardized Tags**: Applies consistent tagging schema across resources
- **Error Handling**: Robust error handling with detailed logging
- **Self-Setup**: Automatically configures CloudAudit tracks if needed

## Applied Tags

### CVM and CDH Tags (Compute Resources)

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerOwner` | Resource owner (email, username, or account ID) | `john.doe@company.com` or `account:1301327510` |
| `TaggerCreated` | Creation date | `2026-02-24` |
| `TaggerAutoOff` | Auto-shutdown flag for power management | `YES` |
| `TaggerAutoStart` | Auto-start flag (requires manual start if NO) | `NO` |
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerTTL` | Time-to-live in days before deletion | `7` |
| `TaggerProject` | Project designation | `n/a` |

**Note**: These tags apply to resources with start/stop operations (CVM instances, CDH hosts).

### CLB Tags (Network Resources)

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerOwner` | Resource owner (email, username, or account ID) | `john.doe@company.com` or `account:1301327510` |
| `TaggerCreated` | Creation date | `2026-02-24` |
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerTTL` | Time-to-live in days before deletion | `7` |
| `TaggerProject` | Project designation | `n/a` |

**Note**: CLB resources can only be created or deleted (no start/stop operations).

### CBS Disk Tags ✅

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerOwner` | Disk creator (email, username, or account ID) | `john.doe@company.com` |
| `TaggerCreated` | Creation date | `2026-02-24` |
| `TaggerUsage` | Disk type (default: SYSTEM) | `SYSTEM` or `DATA` |
| `TaggerLinkedCVM` | Whether disk is attached to a CVM | `YES` or `NO` |
| `TaggerCanDelete` | Auto-deletion flag | `YES` |
| `TaggerTTL` | Time-to-live in days before deletion | `7` |
| `TaggerProject` | Project name (copied from CVM if attached) | `analytics` or `n/a` |

**Note**: CBS disks use `qcs::cvm:region:uin/xxx:volume/disk-id` format for Tag API. Project tags are copied from attached CVM instances when available.

## Architecture

```
CloudAudit (Global) → COS Bucket → SCF Trigger → Tag Resources
```

1. **CloudAudit** captures resource creation events from all regions globally
2. **COS** stores audit logs in structured format  
3. **SCF** processes new log files via COS triggers
4. **Tag API** applies standardized tags to resources

**Note**: CloudAudit tracks are global by default - a single track monitors all regions automatically.

### Supported Events
- `RunInstances` - CVM instances ✅
- `AllocateHosts` - CDH dedicated hosts ✅
- `CreateLoadBalancer` - CLB load balancers ✅
- `CreateCbsStorages` - CBS disk creation ✅
- `AttachDisks` - CBS disk attachment to CVM ✅

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
zip -r scf-tagger.zip index.py package/
```

### 4. Deploy to SCF

1. **Create SCF Function**:
   - Runtime: Python 3.9
   - Handler: `index.main_handler`
   - Memory: 512MB
   - Timeout: 60s

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

#### Track 1: CVM/CBS Track (`tagger-cvm-track`)
- **Events**: `RunInstances`, `AllocateHosts`, `CreateCbsStorages`, `AttachDisks`
- **ResourceType**: `cvm` (CBS events use CVM resource type in CloudAudit)
- **Monitors**: CVM instances, CDH hosts, and CBS disks

#### Track 2: CLB Track (`tagger-clb-track`)
- **Events**: `CreateLoadBalancer`
- **ResourceType**: `clb`
- **Monitors**: CLB load balancers

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

1. Create a new CVM instance, CDH host, CLB load balancer, or CBS disk in any region
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
   - Increase function timeout (default: 60s)
   - Check COS object size and processing time

4. **Permission Errors**:
   - Verify all required IAM policies are attached
   - Check SCF execution role configuration

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
├── index.py                    # Main SCF handler
├── requirements.txt            # Python dependencies
├── policies/                   # IAM policy templates
│   ├── cos-policy.json
│   ├── audit-policy.json
│   └── tag-policy.json
├── package/                    # Dependencies (created by pip install -t)
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
   # Example: Adding NAT Gateway support
   nat_track_name = "tagger-nat-track"
   nat_event_names = ["CreateNatGateway"]
   # ... create track with ResourceType="vpc" ...
   ```

2. **Add Event Handler** (in `main_handler()`):
   ```python
   if event_name == "CreateNatGateway":
       if handle_nat_tagging(rec):
           tagged += 1
       continue
   ```

3. **Implement Tagging Logic**:
   ```python
   def handle_nat_tagging(rec: Dict[str, Any]) -> bool:
       # Extract NAT ID, region, build QCS
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

- **Event Filtering**: Only processes `RunInstances` events to minimize execution
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