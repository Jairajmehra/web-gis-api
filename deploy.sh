#!/bin/bash
# deploy.sh - Automate deployment of the Flask API

# Variables
ZONE="asia-south1-c"
INSTANCE="web-gis-instance"
PROJECT="test-vision-api-389008"
REMOTE_DIR="/home/jairajmehra/web-gis-api"
GIT_REPO="https://github.com/Jairajmehra/web-gis-api.git"
LOG_FILE="$REMOTE_DIR/deployment.log"

# Command to execute on the remote instance.
# This command will:
#   - Change to the project directory.
#   - Run 'git pull' with the given repository, logging output.
#   - Check the exit status of the git pull.
#   - Restart nginx if the git pull succeeded.
REMOTE_COMMANDS=$(cat <<'EOF'
cd /home/jairajmehra/web-gis-api || { echo "ERROR: Directory not found."; exit 1; }
echo "Starting git pull..."
# Run git pull and log both stdout and stderr.
git pull https://github.com/Jairajmehra/web-gis-api.git 2>&1 | tee /home/jairajmehra/web-gis-api/deployment.log
PULL_EXIT=${PIPESTATUS[0]}
if [ $PULL_EXIT -ne 0 ]; then
  echo "ERROR: Git pull failed with exit code $PULL_EXIT. Check deployment.log for details."
  exit $PULL_EXIT
fi
echo "Git pull succeeded. Restarting nginx..."
sudo systemctl restart nginx
if [ $? -ne 0 ]; then
  echo "ERROR: Failed to restart nginx."
  exit 1
fi
echo "Deployment complete."
EOF
)

# Execute the remote commands via gcloud compute ssh.
echo "Connecting to instance and deploying..."
gcloud compute ssh --zone "$ZONE" "$INSTANCE" --project "$PROJECT" --command "$REMOTE_COMMANDS"

# Optionally, you can capture the exit status of the gcloud ssh command.
if [ $? -eq 0 ]; then
  echo "Deployment finished successfully."
else
  echo "Deployment encountered errors."
fi
