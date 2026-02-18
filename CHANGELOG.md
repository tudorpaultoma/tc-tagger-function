# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2025-02-18

### Fixed
- **CloudAudit Multi-Region Architecture**: Corrected implementation to use single global CloudAudit track
  - Fixed `UnsupportedRegion` error when attempting region-specific track creation
  - CloudAudit API calls now correctly use `ap-guangzhou` region (Tencent Cloud requirement)
  - Removed unnecessary `MONITORED_REGIONS` environment variable
  - Single track (`tagger-global-track`) now monitors all regions automatically

### Changed
- Simplified CloudAudit track management from per-region to global approach
- Updated documentation to reflect correct global monitoring architecture
- Removed region-specific track naming and creation logic

### Technical Details
- CloudAudit tracks are inherently global and monitor all regions by default
- CloudAudit API is only available in `ap-guangzhou` region
- Cross-region log delivery to any COS bucket works automatically
- Version bump from 1.2.0 to 1.3.0

## [1.2.0] - 2025-02-17 [YANKED]

**Note**: This version introduced incorrect multi-region architecture and has been superseded by v1.3.0.

### Added
- CDH (Cloud Dedicated Host) auto-tagging support
- `TaggerAutoStart` tag (default: NO) for manual start requirement
- `TaggerTTL` tag (default: 7 days) for automatic deletion scheduling
- Support for `AllocateHosts` events

### Removed
- `TaggerLifeDays` tag (replaced by TaggerTTL)

### Issues
- Attempted per-region CloudAudit track creation (incorrect approach)
- Failed in regions where CloudAudit API is not available

## [1.1.0] - 2025-11-03

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

## [1.1.0] - 2025-11-03

### Added
- **CVM Launch Template Support**: Extended tagging to support CVM Launch Templates
  - Added `CreateLaunchTemplate` event handling
  - Implemented QCS format: `qcs::cvm:region:uin/account:launch-template/template-id`
  - Launch templates now receive the same standardized tags as CVM instances

### Enhanced
- **CloudAudit Track Configuration**: Updated EventNames to include both `RunInstances` and `CreateLaunchTemplate`
- **Event Filtering**: Enhanced `should_tag()` to recognize `createlaunchtemplate` events
- **QCS Extraction**: Added launch template QCS building with fallback mechanisms (resourceSet → responseElements)

### Technical Details
- **Minimal Changes**: Built on proven v1.0.0 codebase with minimal modifications
- **Backward Compatibility**: Maintains full compatibility with existing CVM instance tagging
- **Same Tag Schema**: Launch templates receive identical tags as instances

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