#!/usr/bin/env python3
"""
Simple test for core extraction functions without requiring external dependencies.
This test validates the logic without needing TencentCloud SDKs installed.
"""

import json
import os
import sys

# Mock the TencentCloud SDK imports to avoid import errors
class MockModule:
    def __getattr__(self, name):
        return MockModule()
    
    def __call__(self, *args, **kwargs):
        return MockModule()

sys.modules['qcloud_cos'] = MockModule()
sys.modules['tencentcloud.common'] = MockModule()
sys.modules['tencentcloud.tag.v20180813'] = MockModule()
sys.modules['tencentcloud.cloudaudit.v20190319'] = MockModule()

# Add the parent directory to path to import from index.py
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from index import extract_qcs, get_owner, build_tags, should_tag, extract_region

def test_extraction_functions():
    """Test the core extraction functions with sample data."""
    
    # Load test event
    with open(os.path.join(os.path.dirname(__file__), 'test_event.json'), 'r') as f:
        event = json.load(f)
    
    print("🧪 Testing SCF Resource Tagger extraction functions...")
    print("=" * 60)
    
    # Test should_tag function
    print("📋 Testing should_tag function:")
    should_tag_result = should_tag(event)
    print(f"   Event: {event.get('eventName')}")
    print(f"   Should tag: {should_tag_result}")
    assert should_tag_result == True, "RunInstances event should be tagged"
    print("   ✅ PASS")
    
    # Test extract_region function
    print("\n🌍 Testing extract_region function:")
    region = extract_region(event)
    print(f"   Extracted region: {region}")
    assert region == "ap-guangzhou", f"Expected 'ap-guangzhou', got '{region}'"
    print("   ✅ PASS")
    
    # Test get_owner function
    print("\n👤 Testing get_owner function:")
    owner = get_owner(event)
    print(f"   Extracted owner: {owner}")
    # Should prioritize accountId over principalId
    assert owner == "account:100013033299", f"Expected 'account:100013033299', got '{owner}'"
    print("   ✅ PASS")
    
    # Test build_tags function
    print("\n🏷️  Testing build_tags function:")
    tags = build_tags(owner)
    print(f"   Generated tags:")
    for tag in tags:
        print(f"     {tag['TagKey']}: {tag['TagValue']}")
    
    # Validate tag structure
    assert len(tags) == 5, f"Expected 5 tags, got {len(tags)}"
    tag_keys = [tag['TagKey'] for tag in tags]
    expected_keys = ['TaggerOwner', 'TaggerCreated', 'TaggerLifeDays', 'TaggerAutoOff', 'TaggerProject']
    for key in expected_keys:
        assert key in tag_keys, f"Missing expected tag key: {key}"
    
    # Validate owner tag
    owner_tag = next(tag for tag in tags if tag['TagKey'] == 'TaggerOwner')
    assert owner_tag['TagValue'] == owner, f"Owner tag value mismatch"
    print("   ✅ PASS")
    
    # Test extract_qcs function
    print("\n🔗 Testing extract_qcs function:")
    qcs = extract_qcs(event)
    print(f"   Extracted QCS: {qcs}")
    
    # Validate QCS format
    expected_qcs = "qcs::cvm:eu-frankfurt:uin/100013033299:instance/ins-oks4x5eq"
    assert qcs == expected_qcs, f"Expected '{expected_qcs}', got '{qcs}'"
    print("   ✅ PASS")
    
    print("\n" + "=" * 60)
    print("🎉 All tests passed! The extraction functions are working correctly.")
    print("\n📊 Test Summary:")
    print(f"   Event Name: {event.get('eventName')}")
    print(f"   Resource ID: {event['resourceSet'][0]['resourceId']}")
    print(f"   Region: {region}")
    print(f"   Owner: {owner}")
    print(f"   QCS: {qcs}")
    print(f"   Tags: {len(tags)} applied")

if __name__ == "__main__":
    test_extraction_functions()