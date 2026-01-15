#!/bin/bash
set -e

# Create .env from environment variables (like bitbucket-pipelines.yml)
cat > .env << EOF
DB_HOST=${DB_HOST:-solara.crqioqgga31u.af-south-1.rds.amazonaws.com}
DB_PORT=${DB_PORT:-5432}
DB_NAME=${DB_NAME:-solara}
DB_USER=${DB_USER:-tableau}
DB_PASSWORD=${DB_PASSWORD}
SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN}
SLACK_CHANNEL_ID=${SLACK_CHANNEL_ID}
CLOUDWATCH_LOG_GROUP=${CLOUDWATCH_LOG_GROUP:-/aws/ssm/solara-etl}
CLOUDWATCH_LOG_STREAM=${CLOUDWATCH_LOG_STREAM:-fargate-run}
AWS_REGION=${AWS_REGION:-af-south-1}
EOF

python main.py
