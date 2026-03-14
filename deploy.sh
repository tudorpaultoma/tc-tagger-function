#!/bin/bash

# SCF Resource Tagger Deployment Script
# 
# This script prepares the deployment package for Tencent Cloud SCF
# Usage: ./deploy.sh

set -e

echo "🚀 Building SCF Resource Tagger deployment package..."

# Clean up any existing build artifacts
echo "🧹 Cleaning up previous builds..."
rm -rf package/ build/ dist/ *.zip
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true

# Create package directory for dependencies
echo "📦 Installing dependencies..."
mkdir -p package
pip3 install -r requirements.txt -t package/ --upgrade --no-cache-dir

# Create deployment package - dependencies at root level for SCF
echo "📁 Creating deployment package..."
cd package && zip -r ../scf-tagger.zip . -x "*.pyc" "*/__pycache__/*" "*.DS_Store" && cd ..
zip -g scf-tagger.zip index.py
zip -rg scf-tagger.zip services/ -x "*.pyc" "*/__pycache__/*"

# Display package info
echo "✅ Deployment package created: scf-tagger.zip"
ls -lh scf-tagger.zip

echo ""
echo "📋 Next steps:"
echo "1. Upload scf-tagger.zip to your SCF function"
echo "2. Set handler to: index.main_handler"
echo "3. Configure environment variables:"
echo "   - COS_BUCKET=your-audit-bucket"
echo "   - COS_REGION=your-region"
echo "   - COS_BASE_PREFIX=cloudaudit"
echo "4. Attach required IAM policies (see policies/ directory)"
echo "5. Configure COS trigger for cloudaudit/ prefix"
echo ""
echo "🎉 Ready for deployment!"