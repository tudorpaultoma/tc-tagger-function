#!/usr/bin/env python3
import json
import sys
import os

# Add the parent directory to path to import from index.py
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from index import extract_qcs, get_owner, build_tags

# Load test event
with open(os.path.join(os.path.dirname(__file__), 'test_event.json'), 'r') as f:
    event = json.load(f)

print("Testing QCS extraction:")
qcs = extract_qcs(event)
print(f"QCS: {qcs}")

print("\nTesting owner extraction:")
owner = get_owner(event)
print(f"Owner: {owner}")

print("\nTesting tag building:")
tags = build_tags(owner)
print(f"Tags: {json.dumps(tags, indent=2)}")

print(f"\nExpected QCS: qcs::cvm:eu-frankfurt:uin/100013033299:instance/ins-oks4x5eq")
print(f"Match: {qcs == 'qcs::cvm:eu-frankfurt:uin/100013033299:instance/ins-oks4x5eq'}")