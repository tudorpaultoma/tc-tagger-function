# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.8.1] - 2026-02-25

### Fixed
- **CVM State Polling Resilience**: `wait_for_cvm_running()` now detects `UnauthorizedOperation` immediately instead of retrying for 120s
  - Falls back to timed delay + disk query retries (30s intervals, 90s total) when `cvm:DescribeInstances` permission is missing
  - Prevents wasting entire SCF execution time on permission errors
- **Refactored disk query**: Extracted `_query_and_tag_disks()` and `_query_and_tag_disks_with_retries()` for reuse across both paths

## [1.8.0] - 2026-02-25

### Fixed
- **Region Extraction Bug**: `extract_region()` was returning `eventRegion` (CloudAudit processing region, e.g. `ap-chongqing`) instead of the actual resource region (`eu-frankfurt`). Now prioritizes `resourceRegion` from `resourceSet`, which is the correct region for API calls
  - This was the root cause of CBS disk queries returning empty results — the DescribeDisks API was querying the wrong region

### Changed
- **CVM Disk Tagging — State-Aware Polling**: Replaced blind retry delays with CVM state polling
  - New function `wait_for_cvm_running()` polls `DescribeInstances` until instance is `RUNNING` (10s intervals, 120s max)
  - Disks are attached and queryable once CVM reaches `RUNNING` state
  - Eliminates guesswork on timing; previous fixed delays (60s) were insufficient
  - Added CVM SDK import (`tencentcloud.cvm.v20170312`)
- **Version bump**: 1.7.1 → 1.8.0

### IAM Requirements
- New permission needed: `cvm:DescribeInstances` (for CVM state polling)

## [1.7.1] - 2026-02-25

### Fixed
- **IAM Policy**: Fixed `cbs:DescribeDisks` → `cvm:DescribeDisks` in README (CBS disks are under the `cvm` service namespace)
- **CVM Disk Retry Timing**: Increased retry delays from `[5, 10]` (15s) to `[10, 20, 30]` (60s) with 4 total attempts
  - Disk provisioning can take longer than 15s; previous delays were insufficient

## [1.7.0] - 2026-02-25

### Added
- **CVM System Disk Auto-Tagging**: When a CVM instance is created via `RunInstances`, all attached CBS disks (system and data) are now automatically tagged
  - System disks created alongside CVM instances do not generate separate `CreateCbsStorages` CloudAudit events
  - After tagging the CVM, the function now queries CBS API for disks attached to the instance
  - Each disk is tagged with CBS-specific tags (`TaggerUsage`, `TaggerLinkedCVM=YES`, etc.)
  - Includes retry logic (10s, 20s, 30s delays) for disk provisioning timing
- New function: `tag_cvm_attached_disks(instance_id, region, owner, owner_uin)`
  - Queries `DescribeDisks` with `instance-id` filter
  - Tags each disk found with correct `DiskUsage` (SYSTEM or DATA)
  - Graceful error handling per disk (one failure doesn't block others)

### Changed
- **Version bump**: 1.6.7 → 1.7.0 (minor version bump for new feature)
- `main_handler` now includes CVM disk tagging step after CVM instance tagging
- Instance ID extraction from `resourceSet` or `responseElements` for disk lookup

## [1.6.7] - 2026-02-25

### Fixed
- **CBS Disk Provisioning Race Condition**: Implemented retry mechanism for CBS disk tagging
  - CloudAudit events can arrive before CBS assigns a DiskId to newly created disks
  - Added `find_recent_disk_with_retry()` function with exponential backoff
  - Default retry strategy: 2 attempts with 10s and 20s delays (30s total wait)
  - Significantly improves success rate for console-created CBS disks
  - Handles timing issues where disk provisioning takes 10-30 seconds

### Changed
- **Increased SCF Timeout Recommendation**: For optimal retry behavior, SCF timeout should be increased from 30s to 60s
- Added `time` module import for retry delays
- Enhanced logging to show retry attempts and delays

### Documentation
- **Tag Display Order Clarification**: Updated README and code comments to clarify that tags are displayed **alphabetically** in the Tencent Cloud console
  - This is controlled by the Tag service, not by the order we send tags
  - README tables now show tags in alphabetical order (matching actual console display)
  - Code comments distinguish between "logical order" (for code organization) and "display order" (alphabetical)
  - Added prominent note in README explaining this behavior

### Technical Details
- New function: `find_recent_disk_with_retry(region, event_time, window_seconds=300, max_retries=2, delays=[10, 20])`
- Wraps existing `find_recent_disk()` with configurable retry logic
- Logs each retry attempt with clear status messages
- Graceful degradation: if all retries fail, logs warning and skips tagging (disk can be manually tagged later)

## [1.6.6] - 2026-02-25

### Changed
- **Tag Order Standardization**: Enforced consistent tag ordering across all resource types
  - **CVM/CDH**: TaggerOwner, TaggerCreated, TaggerAutoOff, TaggerAutoStart, TaggerCanDelete, TaggerTTL, TaggerProject
  - **CLB**: TaggerOwner, TaggerCreated, TaggerCanDelete, TaggerTTL, TaggerProject
  - **CBS**: TaggerOwner, TaggerCreated, TaggerUsage, TaggerLinkedCVM, TaggerCanDelete, TaggerTTL, TaggerProject
  - Ensures predictable tag ordering in console and API responses

- **CBS TaggerUsage Default**: Changed default value from DATA to SYSTEM
  - New parameter signature: `disk_usage: str = "SYSTEM"`
  - Reflects common use case where CBS disks are primarily system disks
  - DATA disks still tagged correctly when detected from CBS API

### Documentation
- Updated README tag tables to reflect correct ordering
- Added inline documentation in tag builder functions showing tag order
- Updated version to 1.6.6

## [1.6.5] - 2026-02-24

### Added
- **TaggerCanDelete Tag**: Added `TaggerCanDelete` tag to all resources (CVM, CDH, CLB, CBS)
  - Standardized deletion flag across all resource types
  - Default value: `YES`
  - Replaces CLB's `TaggerDelete` for consistency

### Changed
- **CLB Tags**: Renamed `TaggerDelete` to `TaggerCanDelete` for naming consistency
- **Documentation**: Updated README to reflect CBS full support and latest tag schema
  - Removed "Work in Progress" status from CBS
  - Updated all tag tables with `TaggerCanDelete`
  - Fixed CloudAudit configuration section (2-track system)
  - Updated CBS tagging strategy (removed delays, added console creation handling)

## [1.6.4] - 2026-02-24

### Fixed
- **CBS Event Tracking**: Added CBS events to CVM track
  - CloudAudit classifies CBS events (`CreateCbsStorages`, `AttachDisks`) as `ResourceType="cvm"`, not `"cbs"`
  - Added CBS event names to existing `tagger-cvm-track`
  - No separate CBS track needed

- **Track Validation Bug**: Fixed idempotent track check to validate EventNames
  - Previous validation only checked Status and ResourceType
  - Tracks with outdated EventNames were marked as "valid" and not updated
  - Now validates EventNames match expected configuration
  - Ensures tracks are recreated when event list changes

- **CBS Empty Tag Values**: Fixed empty `TaggerProject` value causing CBS tagging failure
  - CBS API requires all tag values to be non-empty strings
  - Changed empty project from `""` to `"n/a"` for unattached/untagged disks
  - Resolves CBS tag visibility issue reported by Tencent support

- **CBS Console Creation Timing**: Fixed missing disk ID in console-created CBS events
  - CloudAudit event arrives before CBS assigns disk ID (empty `resourceSet`)
  - Added `find_recent_disk()` to query CBS for disks created within 5-minute window
  - Matches disk by creation timestamp proximity to CloudAudit event

- **CBS QCS Format**: Fixed incorrect QCS format preventing tags from being applied
  - CBS disks use `qcs::cvm:region:uin/xxx:volume/disk-id` format (not `qcs::cbs:...`)
  - CBS resources are part of CVM service in Tag API
  - Resource prefix is `volume`, not `disk`
  - Tags now successfully applied to CBS disks

### Changed
- CVM track now monitors: `RunInstances`, `AllocateHosts`, `CreateCbsStorages`, `AttachDisks`
- Architecture remains 2-track system: CVM (includes CBS), CLB
- Track validation: Status=1 AND ResourceType match AND EventNames match
- CBS tags always have non-empty values

## [1.6.3] - 2026-02-23

### Fixed
- **Duplicate CloudAudit Tracks**: Resolved 3x duplicate tagging caused by multiple CVM tracks
  - **Root Cause**: 3 separate CloudAudit tracks (`fra-tagger-track`, `tagger-global-track`, `tagger-cvm-track`) all monitoring CVM events
  - Each track wrote same `RunInstances` event to COS → 3 files → 3 SCF invocations → 3 `TagResources` calls
  - **Solution**: Manual cleanup of duplicate tracks, keep only `tagger-cvm-track` and `tagger-clb-track`
  
- **Resource Extraction Bug**: Fixed tagging wrong resources from `resourceSet`
  - **Problem**: Function was blindly taking `resourceSet[0]` which could be keypair, security group, etc.
  - **Example**: Created CVM but function tagged `skey-3i3pqclv` (keypair) instead of `ins-xxx` (instance)
  - **Solution**: Filter `resourceSet` to find actual instance/host by `resourceTypeClass`
  - CVM: Look for `"Instance"` in `resourceTypeClass` (exclude `"Keypair"`)
  - CDH: Look for `"Host"` in `resourceTypeClass`

### Changed
- **Idempotent Track Management**: Only create/update tracks when necessary
  - Check if track exists and is valid (Status=1, correct ResourceType)
  - Skip deletion/recreation if track is already correctly configured
  - Only delete track if it's misconfigured or disabled
  - Logs `"action": "skip_recreation"` when track is valid

- `extract_qcs()` now iterates through `resourceSet` to find correct resource type
- Only processes resources matching expected type (Instance/Host), not related resources

### Technical Details
- CloudAudit duplicate tracks investigation:
  - Old code (pre-v1.6.2) deleted/recreated tracks on every invocation → left orphaned tracks
  - Multiple deployments created `fra-tagger-track`, `tagger-global-track`, `tagger-cvm-track`
  - All 3 active tracks captured same events → multiplied event delivery by 3
- CloudAudit `resourceSet` contains ALL resources involved in operation:
  - `resourceSet[0]`: Could be keypair, security group, VPC, etc.
  - `resourceSet[N]`: The actual instance/host is somewhere in the array
- Previous logic: `resourceSet[0].resourceId` ❌
- New logic: `for r in resourceSet if "Instance" in r.resourceTypeClass` ✅
- Track validity: `Status=1` AND `ResourceType` matches (cvm/clb)

### Impact
- ✅ Eliminates 3x duplicate tagging (now only 1 TagResources event per resource)
- ✅ Prevents CloudAudit track churn (no more constant delete/recreate)
- ✅ Reduces CloudAudit noise and API quota usage
- ✅ Stable track configuration across all invocations
- ✅ Correct resource tagging (instances, not related resources)

## [1.5.1] - 2026-02-22

### Fixed
- **CloudAudit Track Configuration**: Switched to separate tracks per service type
  - CloudAudit API requires specific `ResourceType` (does not support wildcard `*` with EventNames)
  - Track 1: `tagger-cvm-track` → monitors CVM/CDH (`RunInstances`, `AllocateHosts`)
  - Track 2: `tagger-clb-track` → monitors CLB (`CreateLoadBalancer`)
  - Both tracks deliver to same COS bucket → single SCF function processes all events

### Changed
- **Architecture**: Multi-track approach replaces single global track
  - Each service type gets dedicated CloudAudit track
  - Cleaner separation of concerns
  - Easier to add new service types in future
  
### Added
- **IAM Permission**: `cloudaudit:DeleteAuditTrack` added to audit-policy.json
  - Required for track recreation during configuration updates

### Technical Details
- CloudAudit limitation discovered: `ResourceType: "*"` + `EventNames` is invalid
- Track deletion/recreation logic replaces update logic (simpler, more reliable)
- Function now manages multiple track lifecycles

### Known Issues
- **CBS Tagging**: Multiple blockers prevent CBS auto-tagging:
  1. CloudAudit does not support CBS event names (API returns "illegal params")
  2. CBS Tag API returns success but CBS service doesn't honor tags
  3. Tags appear in Tag service but not in CBS console/API
  - Support ticket submitted to Tencent Cloud for both issues

## [1.5.0] - 2026-02-22

### Added
- **CLB (Cloud Load Balancer) Support**: Automatic tagging for load balancers ✅
  - Handles `CreateLoadBalancer` CloudAudit events
  - Full Tag API support (verified working)
  - CLB-specific tag schema (TaggerOwner, TaggerCreated, TaggerTTL, TaggerDelete, TaggerProject)
  - Uses `TaggerDelete` instead of `TaggerAutoOff`/`TaggerAutoStart` (CLBs can only be created/deleted)
  - High-cost resource prioritization for better cost tracking

### Changed
- CloudAudit track now monitors CLB events: `CreateLoadBalancer`
- Updated README with CLB support and CBS limitation status
- Separated tag documentation by resource type (Compute vs Network vs Storage)
- Version bump from 1.4.0 to 1.5.0

### Technical Details
- New `build_clb_tags()` function for CLB-specific tag generation
- New `handle_clb_tagging()` function for CLB event processing
- New `extract_account_uin()` helper function for UIN extraction
- QCS format: `qcs::clb:{region}:uin/{uin}:clb/{lb_id}`
- Load balancer ID extraction from `resourceSet` and `responseElements.LoadBalancerIds`
- Immediate tagging on creation (no delay needed)

### Known Issues
- **CBS Tagging Limitation**: CBS disk tagging remains work in progress
  - Tag API returns success but CBS service doesn't honor tags
  - Tags visible in Tag service but not in CBS console/API
  - Support ticket submitted to Tencent Cloud (#SUPPORT_TICKET.md)
  - Marked as ⚠️ in documentation

## [1.4.0] - 2025-02-20

### Added
- **CBS (Cloud Block Storage) Support**: Automatic tagging for CBS disks
  - Handles `CreateCbsStorages` and `AttachDisks` CloudAudit events
  - Smart tagging strategy based on disk attachment state
  - Added `TaggerUsage` tag (SYSTEM or DATA) to identify disk type
  - Added `TaggerLinkedCVM` tag (YES or NO) to track CVM attachment
  
### Features
- **Attached Disk Tagging**: Copies `TaggerProject` from associated CVM
  - Recreates `TaggerOwner`, `TaggerCreated`, `TaggerTTL` from audit event
  - Waits 10 minutes for CVM tags to propagate before tagging
  
- **Unattached Disk Tagging**: Applies default tags with empty project
  - 10-minute grace period before tagging (allows user to attach)
  
- **Re-attachment Support**: Updates tags when disk moved between CVMs
  - `AttachDisks` event triggers project tag update from new CVM

### Changed
- CloudAudit track now monitors CBS events: `CreateCbsStorages`, `AttachDisks`
- CBS disks excluded from receiving `TaggerAutoOff` and `TaggerAutoStart` tags
- Version bump from 1.3.0 to 1.4.0

### Technical Details
- **Event Name Discovery**: CloudAudit uses `CreateCbsStorages` (not `CreateDisks`) for CBS creation
- New CBS API integration (`tencentcloud.cbs.v20170312`)
- Added `get_disk_info()` - query disk state, attachment, usage type
- Added `get_cvm_tags()` - read tags from CVM instances
- Added `build_cbs_tags()` - generate CBS-specific tag set
- Added `handle_cbs_tagging()` - main CBS event handler with 10-minute age check
- Disk ID extraction from `resourceSet` field in console-created events
- Disk age validation prevents premature tagging

## [1.3.0] - 2025-02-18

### Fixed
- **CloudAudit Multi-Region Architecture**: Corrected implementation to use single global CloudAudit track
  - Fixed `UnsupportedRegion` error when attempting region-specific track creation
  - CloudAudit API calls use European region endpoint (configurable, defaults to eu-frankfurt)
  - Removed unnecessary `MONITORED_REGIONS` environment variable
  - Single track (`tagger-global-track`) now monitors all regions automatically

### Changed
- Simplified CloudAudit track management from per-region to global approach
- Updated documentation to reflect correct global monitoring architecture
- Removed region-specific track naming and creation logic

### Technical Details
- CloudAudit tracks are inherently global and monitor all regions by default
- CloudAudit API configured to use European region endpoint (eu-frankfurt)
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