# SCF Resource Tagger

An automated Tencent Cloud Serverless Cloud Function (SCF) that automatically tags newly created cloud resources based on CloudAudit events delivered to COS (Cloud Object Storage).

## Overview

This SCF function monitors CloudAudit logs stored in COS and automatically applies standardized tags to newly created cloud resources. When supported creation events are detected, the function extracts resource information and applies tags for better resource management and cost tracking.

## Supported Services

### 🖥️ **CVM (Cloud Virtual Machine)**
- **Instances** - `RunInstances` events
- **Launch Templates** - `CreateLaunchTemplate` events

## Features

- **CVM Support**: Tags CVM instances and launch templates automatically
- **Automatic Tagging**: Tags resources immediately after creation
- **CloudAudit Integration**: Processes CloudAudit events from COS storage
- **Flexible Owner Detection**: Prioritizes email, username, account ID, or UIN for owner identification
- **Standardized Tags**: Applies consistent tagging schema across resources
- **Error Handling**: Robust error handling with detailed logging
- **Self-Setup**: Automatically configures CloudAudit tracks if needed

## Applied Tags

The function applies the following tags to all supported resources:

| Tag Key | Description | Example Value |
|---------|-------------|---------------|
| `TaggerOwner` | Resource owner (email, username, or account ID) | `john.doe@company.com` or `account:1301327510` |
| `TaggerCreated` | Creation date | `2025-10-27` |
| `TaggerLifeDays` | Default lifecycle in days | `1` |
| `TaggerAutoOff` | Auto-shutdown flag | `YES` |
| `TaggerProject` | Project designation | `n/a` |

## Architecture

```
CloudAudit → COS Bucket → SCF Trigger → Tag Resources
```

1. **CloudAudit** captures resource creation events (CVM, CBS, etc.)
2. **COS** stores audit logs in structured format  
3. **SCF** processes new log files via COS triggers
4. **Tag API** applies standardized tags to resources

### Supported Events
- `RunInstances` - CVM instances
- `CreateLaunchTemplate` - CVM launch templates

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
| `COS_REGION` | Yes | - | COS bucket region |
| `COS_BASE_PREFIX` | No | `cloudaudit` | Base prefix for audit logs |
| `AUDIT_SETUP` | No | `true` | Auto-setup CloudAudit tracks |

### CloudAudit Configuration

The function automatically configures CloudAudit tracks with:
- **Track Name**: `{region-short}-tagger-track` (e.g., `fra-tagger-track`)
- **Event Filter**: `RunInstances` and `CreateLaunchTemplate` events from CVM service
- **Storage**: COS bucket with prefix `cloudaudit/{region}`

## Usage

### Automatic Operation

Once deployed and configured, the function operates automatically:

1. Create a new CVM instance in any region
2. CloudAudit captures the `RunInstances` event
3. Event is stored in COS bucket
4. SCF function is triggered by new COS object
5. Function processes the event and tags the instance

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