#!/bin/bash
set -e

cd /home/ec2-user/etl_project

echo "Ensuring Docker and Git are installed..."
sudo yum install -y docker git

echo "Starting Docker daemon..."
sudo systemctl start docker

echo "Building Docker image..."
docker build -t solara-etl:latest .
so you
echo "Running ETL pipeline..."
docker run --rm \
  -e DB_PASSWORD="${DB_PASSWORD}" \
  -e AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID}" \
  -e AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY}" \
  -e CLOUDWATCH_LOG_GROUP=/aws/ssm/solara-etl \
  -e CLOUDWATCH_LOG_STREAM=etl-pipeline \
  -v ~/.aws:/root/.aws:ro \
  solara-etl:latest

echo "ETL pipeline completed successfully!"
