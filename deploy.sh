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

# Create package directory for dependencies
echo "📦 Installing dependencies..."
mkdir -p package
pip install -r requirements.txt -t package/ --no-deps

# Create deployment package
echo "📁 Creating deployment package..."
zip -r scf-tagger.zip index.py package/ -x "*.pyc" "*/__pycache__/*" "*.DS_Store"

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