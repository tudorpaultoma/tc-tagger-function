# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2025-10-27

### Added
- Initial release of SCF Resource Tagger
- Automatic tagging of CVM instances on creation
- CloudAudit integration for event processing
- COS trigger support for real-time processing
- Comprehensive error handling and logging
- Self-setup of CloudAudit tracks
- Support for multiple owner identification methods (email, username, account ID, UIN)
- Standardized tagging schema with 5 default tags
- Type safety and input validation
- IAM policy templates for easy deployment
- Test files for local development and validation

### Features
- **Event Processing**: Monitors `RunInstances` events from CloudAudit
- **Owner Detection**: Prioritizes email > username > account ID > UIN
- **QCS Generation**: Builds proper Tencent Cloud QCS strings for resource identification
- **Region Support**: Works across all Tencent Cloud regions
- **Batch Processing**: Handles multiple events in single invocation
- **Debug Logging**: Comprehensive JSON logging for troubleshooting

### Tags Applied
- `TaggerOwner`: Resource owner identification
- `TaggerCreated`: Creation date in ISO format
- `TaggerLifeDays`: Default lifecycle (1 day)
- `TaggerAutoOff`: Auto-shutdown flag (YES)
- `TaggerProject`: Project designation (n/a)

### Technical Details
- **Runtime**: Python 3.9+
- **Memory**: 512MB recommended
- **Timeout**: 60 seconds
- **Dependencies**: TencentCloud SDK, COS SDK
- **Trigger**: COS object creation events
- **Storage**: CloudAudit logs in COS with `cloudaudit/` prefix

### Security
- Uses SCF execution role for authentication
- Implements least privilege access patterns
- Validates all input data types
- No hardcoded credentials or sensitive data logging

### Documentation
- Comprehensive README with setup instructions
- IAM policy templates
- Troubleshooting guide
- Architecture diagrams
- Local development setup

## [Unreleased]

### Planned Features
- Support for additional resource types (CLB, VPC, etc.)
- Custom tagging rules configuration
- Tag validation and compliance checking
- Integration with cost management systems
- Webhook notifications for tagging events
- Bulk retagging of existing resources
- Tag inheritance from parent resources
- Advanced filtering and conditional tagging

### Potential Improvements
- Performance optimization for large-scale deployments
- Enhanced error recovery mechanisms
- Support for custom tag schemas
- Integration with CMDB systems
- Automated tag lifecycle management
- Multi-account support
- Tag governance and policy enforcement