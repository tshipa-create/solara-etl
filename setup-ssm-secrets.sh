#!/bin/bash
set -e

AWS_REGION=${AWS_REGION:-af-south-1}

echo "Setting up SSM Parameter Store secrets for Solara ETL..."
echo "Region: ${AWS_REGION}"
echo ""

# DB Password
echo "Enter PostgreSQL password (will be stored in /solara-etl/db-password):"
read -s DB_PASSWORD

echo ""
echo "Setting DB password in SSM..."
aws ssm put-parameter \
  --name /solara-etl/db-password \
  --value "${DB_PASSWORD}" \
  --type SecureString \
  --overwrite \
  --region "${AWS_REGION}"
echo "✓ DB password set"

echo ""

# Slack Bot Token
echo "Enter Slack bot token (will be stored in /solara-etl/slack-bot-token):"
read -s SLACK_BOT_TOKEN

echo ""
echo "Setting Slack bot token in SSM..."
aws ssm put-parameter \
  --name /solara-etl/slack-bot-token \
  --value "${SLACK_BOT_TOKEN}" \
  --type SecureString \
  --overwrite \
  --region "${AWS_REGION}"
echo "✓ Slack bot token set"

echo ""
echo "✓ All secrets configured in SSM Parameter Store"
echo ""
echo "You can now remove DB_PASSWORD and SLACK_BOT_TOKEN from Bitbucket Repository Variables"
