#!/bin/bash
set -e

echo "=== Solara ETL EC2 Initialization ==="

export AWS_REGION=af-south-1

sudo yum update -y

echo "Installing Docker..."
sudo yum install -y docker
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker ec2-user

echo "Installing AWS CLI v2..."
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install
rm -rf aws awscliv2.zip

echo "Installing Git..."
sudo yum install -y git

echo "Creating project directory..."
mkdir -p /home/ec2-user/etl_project
cd /home/ec2-user/etl_project

echo "Creating .env file structure..."
cat > /home/ec2-user/etl_project/.env.template <<'EOF'
AWS_REGION=af-south-1
DB_HOST=solara.crqioqgga31u.af-south-1.rds.amazonaws.com
DB_PORT=5432
DB_NAME=solara
DB_USER=tableau
DB_PASSWORD=<fetch from SSM>
CLOUDWATCH_LOG_GROUP=/aws/ssm/solara-etl
CLOUDWATCH_LOG_STREAM=etl-pipeline
EOF

echo "Setting permissions..."
sudo chown -R ec2-user:ec2-user /home/ec2-user/etl_project
sudo chmod 755 /home/ec2-user/etl_project

echo "=== EC2 Initialization Complete ==="
echo "Next steps:"
echo "1. Clone the repository: git clone <repo-url> /home/ec2-user/etl_project"
echo "2. Configure .env with SSM parameters"
echo "3. Pull and run the Docker image: docker pull solara-etl:latest"
