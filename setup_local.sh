#!/bin/bash

# Set environment variables for local development
export GOOGLE_APPLICATION_CREDENTIALS="credentials/key.json"
export BUCKET_NAME="web-gis-2198"

echo "Environment variables set:"
echo "GOOGLE_APPLICATION_CREDENTIALS=$GOOGLE_APPLICATION_CREDENTIALS"
echo "BUCKET_NAME=$BUCKET_NAME"

# Activate virtual environment if it exists
if [ -d "web-gis-env" ]; then
    source web-gis-env/bin/activate
fi 