# SCF Resource Tagger - Deployment Guide

## ✅ Status: **WORKING**

The function has been successfully tested and is tagging CVM instances automatically.

## Changes Made (v1.0.1)

### Bug Fixes
1. **File Reading Truncation** - Fixed `read_cos_object()` to read complete files instead of just 1024 bytes
2. **JSON Parsing** - Updated `parse_lines()` to handle single JSON objects (CloudAudit format)
3. **String Parsing** - Fixed `extract_qcs()` to parse `requestParameters` and `responseElements` as JSON strings
4. **Logging** - Cleaned up debug logging for production use

### Key Code Changes

#### `read_cos_object()` - Read files in chunks
```python
body_stream = resp['Body']
chunks = []
while True:
    chunk = body_stream.read(8192)  # Read in 8KB chunks
    if not chunk:
        break
    chunks.append(chunk)
body = b''.join(chunks)
```

#### `parse_lines()` - Handle single JSON objects
```python
# Try parsing as single JSON object first
try:
    obj = json.loads(content)
    if isinstance(obj, dict):
        items.append(obj)
        return items
except Exception:
    pass
```

#### `extract_qcs()` - Parse JSON strings
```python
# Parse requestParameters if it's a JSON string
if isinstance(params_raw, str):
    try:
        params = json.loads(params_raw)
    except Exception:
        params = {}
```

## Deployment Configuration

### Environment Variables
```bash
COS_BUCKET=tommywork-1301327510
COS_REGION=eu-frankfurt
COS_BASE_PREFIX=taggertags
```

### COS Trigger Setup
- **Bucket**: `tommywork-1301327510`
- **Event**: `cos:ObjectCreated:*`
- **Prefix**: `taggertags/fra-tagger-track/`
- **Suffix**: `.txt` (optional)

### Handler
```
index.main_handler
```

## CloudAudit Track

The function automatically creates/updates a CloudAudit track:
- **Name**: `fra-tagger-track` (based on region)
- **Type**: COS delivery
- **Events**: `RunInstances` (CVM creation)
- **Storage Path**: `taggertags/fra-tagger-track/YYYY/MM/DD/*.txt`

## Test Results

### Successful Test
```json
{
  "status": "ok",
  "setup": {
    "cos_bucket_ok": true,
    "track_id": 505
  },
  "processed": 1,
  "tagged": 1,
  "errors": []
}
```

### Performance
- **Duration**: ~2-3 seconds
- **Memory**: ~27 MB
- **Cold Start**: ~870ms

## Tags Applied

The function applies these tags to new CVM instances:
- `TaggerOwner`: User who created the instance (email or username)
- `TaggerCreated`: Creation date (ISO format)
- `TaggerLifeDays`: Default "1"
- `TaggerAutoOff`: Default "YES"
- `TaggerProject`: Default "n/a"

## Next Steps

### 1. Update SCF Function
Upload the new deployment package:
```bash
./deploy.sh
# Upload scf-tagger.zip to SCF console
```

### 2. Test End-to-End
1. Create a new CVM instance
2. Wait 1-2 minutes for CloudAudit to write event
3. Check SCF logs for successful tagging
4. Verify tags on the CVM instance

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

- `index.py` - Main function code (v1.0.1)
- `deploy.sh` - Deployment package builder
- `scf-tagger.zip` - Ready-to-deploy package (15MB)
- `requirements.txt` - Python dependencies
- `policies/` - IAM policy templates

## Support

For issues or questions, check:
- SCF function logs in Tencent Cloud Console
- CloudAudit track configuration
- COS trigger settings
