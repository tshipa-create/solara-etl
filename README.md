# Solara ETL

Automated ETL pipeline for syncing Solara PostgreSQL data to Snowflake, running on AWS ECS Fargate with Infrastructure-as-Code.

## Quick Start

### Prerequisites
- AWS account with appropriate IAM permissions
- Bitbucket repository access
- PostgreSQL RDS database
- Snowflake account
- Slack workspace (for notifications)

### One-Time Setup

1. **Create Terraform backend** (S3 + DynamoDB):
```powershell
aws s3api create-bucket --bucket solara-etl-terraform-state --region af-south-1 --create-bucket-configuration LocationConstraint=af-south-1
aws dynamodb create-table --table-name terraform-locks --attribute-definitions AttributeName=LockID,AttributeType=S --key-schema AttributeName=LockID,KeyType=HASH --billing-mode PAY_PER_REQUEST --region af-south-1
```

2. **Set Bitbucket Repository Variables** (Settings → Repository Variables):
```
AWS_REGION = af-south-1
AWS_ACCESS_KEY_ID = AKIA...
AWS_SECRET_ACCESS_KEY = ...
TF_STATE_BUCKET = solara-etl-terraform-state
DB_HOST = your-rds-host.af-south-1.rds.amazonaws.com
DB_USER = tableau
DB_PASSWORD = your_postgres_password
SLACK_CHANNEL_ID = C123456789ABC
SLACK_BOT_TOKEN = xoxb-your-slack-token
```

3. **Push to main branch**:
```bash
git push origin main
```

Pipeline automatically runs and deploys infrastructure.

## Architecture

```
Bitbucket (CI/CD)
    ↓
  Docker Build & Push to ECR
    ↓
  Verify SSM Secrets
    ↓
  Terraform Apply (creates ECS infrastructure)
    ↓
  EventBridge Rule (cron: every 2 hours)
    ↓
  ECS Fargate Task
    ↓
  PostgreSQL → Snowflake Sync
    ↓
  Slack Notification (with CloudWatch logs link)
```

### Components

- **ECR**: Container registry for Docker images
- **ECS Fargate**: Serverless container execution
- **EventBridge**: Scheduled cron trigger
- **SSM Parameter Store**: Encrypted secrets storage
- **CloudWatch**: Logs and monitoring
- **Terraform**: Infrastructure-as-Code

## Deployment Workflow

### Feature Branches (Development)
```bash
git checkout -b feature/add-new-table
# Make code changes
git commit -m "feat: add new table sync logic"
git push origin feature/add-new-table
```
→ Pipeline builds and tests, **no infrastructure changes**

### Main Branch (Production)
```bash
git checkout main
git merge feature/add-new-table
git push origin main
```
→ Pipeline **automatically**:
1. Builds Docker image
2. Pushes to ECR
3. Verifies secrets exist
4. Runs Terraform (applies infrastructure changes)

## Updating Configuration

### Change Database Host
```bash
# 1. Update Bitbucket variable: DB_HOST = new-host.af-south-1.rds.amazonaws.com
# 2. Commit any code changes
git push origin main
# 3. Pipeline auto-deploys with new host
```

### Change Schedule (Sync Frequency)
Edit `terraform/variables.tf`:
```hcl
variable "schedule_expression" {
  type    = string
  default = "cron(0 */6 * * ? *)"  # Change from every 2 hours to every 6 hours
}
```
```bash
git push origin main
```

### Change Task Resources (CPU/Memory)
Edit `terraform/variables.tf`:
```hcl
variable "task_cpu" {
  type    = string
  default = "1024"  # Increase from 512
}

variable "task_memory" {
  type    = number
  default = 2048  # Increase from 1024
}
```
```bash
git push origin main
```

## Monitoring & Logs

### View CloudWatch Logs
```bash
aws logs describe-log-groups --region af-south-1 | grep solara-etl
aws logs describe-log-streams --log-group-name /aws/ssm/solara-etl --region af-south-1
aws logs tail /aws/ssm/solara-etl --follow --region af-south-1
```

### View Latest Task Execution
```bash
aws ecs list-tasks --cluster solara-etl-cluster --region af-south-1
aws ecs describe-tasks --cluster solara-etl-cluster --tasks <task-arn> --region af-south-1
```

### View EventBridge Schedule
```powershell
aws events describe-rule --name solara-etl-schedule --region af-south-1
```

### Slack Notifications
- ✅ Success: Sync count + table names
- 🚨 Failure: Error details + affected tables
- 📋 Clickable link to CloudWatch logs

## Troubleshooting

### Pipeline Fails: "Docker daemon not running"
→ Ensure `services: docker` is defined in `bitbucket-pipelines.yml` and service is enabled in Bitbucket settings.

### Pipeline Fails: "Terraform command not found"
→ Check terraform installation steps completed. Verify terraform binary in `/usr/local/bin/terraform`.

### ECS Task Fails: "Cannot connect to database"
→ Check security group allows outbound to RDS on port 5432. Verify DB_HOST in SSM parameter.

### ECS Task Fails: "SSM secret not found"
→ Check `/solara-etl/db-password` and `/solara-etl/slack-bot-token` exist in SSM Parameter Store.

### Slack notification not received
→ Verify `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` in SSM parameters. Check Slack bot has permission to post in channel.

## Key Files

| File | Purpose |
|------|---------|
| `bitbucket-pipelines.yml` | CI/CD pipeline configuration |
| `terraform/main.tf` | ECS, EventBridge, IAM resources |
| `terraform/variables.tf` | Configuration variables (CPU, memory, schedule, database, Slack) |
| `main.py` | ETL logic: PostgreSQL → Snowflake sync |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Container image definition |
| `entrypoint.sh` | Task startup script |

## Deprecated Files

The following are no longer used. Do not modify:
- `deploy.sh`
- `deploy_lambda.py`
- `deploy_fargate.py`
- `lambda_deploy.yaml`
- `fargate_deploy.yaml`
- `setup-ssm-secrets.sh`

All deployment is now handled by Bitbucket Pipelines + Terraform.



