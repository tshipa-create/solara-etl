# Solara ETL Pipeline Deployment

## Architecture

```
Bitbucket Push → Docker Build → ECR Push → ECS Fargate Task
                                             ↓
                            EventBridge (every 2 hours)
                                             ↓
                         PostgreSQL RDS → Snowflake
                                             ↓
                            CloudWatch Logs + Slack Alerts
```

- **Scheduling**: AWS EventBridge (cron-based)
- **Execution**: ECS Fargate (serverless container)
- **Logging**: CloudWatch Logs (30-day retention)
- **Secrets**: AWS SSM Parameter Store (encrypted)

## Setup (One-Time)

### 1. AWS Terraform State Backend

Create S3 bucket and DynamoDB lock table:
```bash
aws s3 mb s3://solara-etl-terraform-state --region af-south-1
aws dynamodb create-table \
  --table-name terraform-locks \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --provisioned-throughput ReadCapacityUnits=1,WriteCapacityUnits=1 \
  --region af-south-1
```

### 2. AWS CloudWatch Log Group

Create log group (Terraform will reference it):
```bash
aws logs create-log-group --log-group-name "/aws/ssm/solara-etl" --region af-south-1
aws logs put-retention-policy --log-group-name "/aws/ssm/solara-etl" --retention-in-days 30 --region af-south-1
```

### 3. Bitbucket Repository Variables

Set these in **Settings → Repository Variables**:

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

These are automatically created in AWS SSM Parameter Store during deployment.

## Deployment Workflow

### Development (Feature Branches)

**1. Create feature branch:**
```bash
git checkout -b feature/my-change
```

**2. Make code changes** (e.g., modify `main.py`, add dependencies)

**3. Commit changes:**
```bash
git add .
git commit -m "feat: add new table sync logic"
```

**4. Push to feature branch:**
```bash
git push origin feature/my-change
```

**Pipeline runs:**
- Builds Docker image with tag `<commit-hash>`
- Pushes to ECR as `latest`
- Verifies SSM secrets exist
- **Does NOT update infrastructure**

**5. Create Pull Request:**
- Go to Bitbucket repository
- Click "Create Pull Request" 
- Review changes and merge to `main`

### Production (Main Branch)

**1. Merge to main:**
```bash
git checkout main
git pull origin main
```

Or merge PR in Bitbucket UI

**2. Pipeline automatically runs full deployment:**
- Builds Docker image with commit hash tag
- Pushes to ECR as `latest`
- Verifies SSM secrets exist
- **Updates ECS task definition** with new image
- **EventBridge triggers next scheduled run** with new code

**3. Verify deployment:**
```bash
aws ecs describe-task-definition \
  --task-definition solara-etl \
  --region af-south-1 \
  --query 'taskDefinition.containerDefinitions[0].image' \
  --output text
```

**4. Monitor execution:**
```bash
aws logs tail /aws/ssm/solara-etl --follow --region af-south-1
```

### Infrastructure Changes

**1. Edit Terraform files** (e.g., schedule, CPU, memory):
```bash
git checkout -b infra/update-schedule
# Edit terraform/variables.tf
git add terraform/variables.tf
git commit -m "infra: change schedule to hourly"
git push origin infra/update-schedule
```

**2. Create PR and merge to `main`**

**3. Pipeline applies changes:**
- Updates ECS cluster
- Reconfigures EventBridge rule
- Updates CloudWatch configuration
- No downtime - updates happen in-place

## Rollback

**If deployment breaks:**

**Option 1: Revert code**
```bash
git revert <commit-hash>
git push origin main
# Pipeline redeploys with previous version
```

**Option 2: Manual rollback**
```bash
aws ecs update-service \
  --cluster solara-etl-cluster \
  --service solara-etl \
  --task-definition solara-etl:<previous-revision> \
  --region af-south-1
```

## Quick Reference

| Branch | Docker Build | Secrets Check | Infrastructure |
|--------|:---:|:---:|:---:|
| feature/* | Yes | Yes | No |
| main | Yes | Yes | Yes |

## Monitoring

### View Logs in Real-Time
```bash
aws logs tail /aws/ssm/solara-etl --follow --region af-south-1
```

### Get Last 100 Lines of Logs
```bash
aws logs tail /aws/ssm/solara-etl --max-items 100 --region af-south-1
```

### View Specific Time Range
```bash
aws logs filter-log-events \
  --log-group-name /aws/ssm/solara-etl \
  --start-time $(($(date +%s%N)/1000000 - 3600000)) \
  --region af-south-1
```

### Check EventBridge Status
```bash
aws events describe-rule --name solara-etl-schedule --region af-south-1
```

### View Scheduled Events
```bash
aws events list-targets-by-rule --rule solara-etl-schedule --region af-south-1
```

### Manual Task Run (On-Demand)
```bash
CLUSTER=$(terraform output -raw cluster_name)
TASK_DEF=$(terraform output -raw task_definition_arn)
aws ecs run-task \
  --cluster $CLUSTER \
  --task-definition $TASK_DEF \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={assignPublicIp=DISABLED}" \
  --region af-south-1
```

### Check Task Status
```bash
aws ecs list-tasks \
  --cluster solara-etl-cluster \
  --region af-south-1
```

### View Task Details
```bash
aws ecs describe-tasks \
  --cluster solara-etl-cluster \
  --tasks <task-arn> \
  --region af-south-1
```

### Check Docker Image Currently Deployed
```bash
aws ecs describe-task-definition \
  --task-definition solara-etl \
  --region af-south-1 \
  --query 'taskDefinition.containerDefinitions[0].image'
```

## Configuration

### Change Execution Schedule

**1. Edit Terraform variables:**
```bash
git checkout -b infra/update-schedule
# Edit terraform/variables.tf
```

**2. Update schedule:**
```hcl
variable "schedule_expression" {
  type    = string
  default = "cron(0 * * * ? *)"  # Change this
}
```

**3. Commit and push to main:**
```bash
git add terraform/variables.tf
git commit -m "infra: change schedule to hourly"
git push origin infra/update-schedule
# Create PR and merge to main
```

**4. Pipeline applies new schedule automatically**

**Common schedules:**

| Frequency | Cron Expression |
|-----------|---|
| Hourly | `cron(0 * * * ? *)` |
| Every 2 hours | `cron(0 */2 * * ? *)` |
| Every 6 hours | `cron(0 */6 * * ? *)` |
| Daily at 2 AM UTC | `cron(0 2 * * ? *)` |
| Every weekday 6 AM | `cron(0 6 ? * MON-FRI *)` |

### Change Task Resources (CPU/Memory)

**1. Edit Terraform variables:**
```bash
git checkout -b infra/increase-resources
```

**2. Update resources:**
```hcl
variable "task_cpu" {
  type    = string
  default = "1024"  # 256, 512, 1024, 2048, 4096
}

variable "task_memory" {
  type    = number
  default = 2048  # MB, must match CPU
}
```

**3. Commit and push:**
```bash
git add terraform/variables.tf
git commit -m "infra: increase CPU to 1024 and memory to 2048MB"
git push origin infra/increase-resources
# Create PR and merge to main
```

**4. Pipeline updates task definition automatically**

### Change Database Connection

**1. Update Bitbucket repository variables** (Settings → Repository Variables):
```
DB_HOST = new-rds-host.af-south-1.rds.amazonaws.com
DB_USER = new_user
```

**2. No code changes needed** - pipeline picks up new variables on next deploy

### Change Slack Channel

**1. Update Bitbucket variable:**
```
SLACK_CHANNEL_ID = C987654321XYZ
```

**2. Next deployment sends alerts to new channel**

## Pre-Deployment Checklist

- [ ] Terraform state S3 bucket exists
- [ ] DynamoDB lock table exists
- [ ] CloudWatch log group `/aws/ssm/solara-etl` exists
- [ ] Bitbucket repository variables set:
  - [ ] AWS_ACCESS_KEY_ID
  - [ ] AWS_SECRET_ACCESS_KEY
  - [ ] TF_STATE_BUCKET
  - [ ] DB_HOST
  - [ ] DB_USER
  - [ ] DB_PASSWORD
  - [ ] SLACK_CHANNEL_ID
  - [ ] SLACK_BOT_TOKEN

## Troubleshooting

### Verify Secrets Were Created by Terraform
```bash
aws ssm get-parameter --name /solara-etl/db-password --with-decryption --region af-south-1
aws ssm get-parameter --name /solara-etl/slack-bot-token --with-decryption --region af-south-1
```

If not found, check that `DB_PASSWORD` and `SLACK_BOT_TOKEN` Bitbucket variables are set and deployment succeeded.

### Verify Log Group Exists
```bash
aws logs describe-log-groups --log-group-name-prefix "/aws/ssm/solara-etl" --region af-south-1
```

### Task Failed
Check logs:
```bash
aws logs tail /aws/ssm/solara-etl --follow --region af-south-1
```

### Manual Re-Deploy
```bash
cd terraform
terraform init
terraform apply \
  -var "container_image=<ECR_IMAGE>" \
  -var "db_host=<DB_HOST>" \
  -var "db_user=<DB_USER>" \
  -var "slack_channel_id=<SLACK_CHANNEL>"
```

## Current Files (Use These)

| File | Purpose |
|------|---------|
| `bitbucket-pipelines.yml` | CI/CD automation (Bitbucket → Docker → ECR → ECS) |
| `terraform/main.tf` | Infrastructure definition (ECS Fargate + EventBridge + SSM Secrets) |
| `terraform/variables.tf` | Configuration parameters |
| `terraform/outputs.tf` | Output values for Terraform |
| `Dockerfile` | Container image definition |
| `entrypoint.sh` | Container startup script |
| `main.py` | ETL pipeline logic |
| `requirements.txt` | Python dependencies |

## Deprecated Files (Do Not Use)

These files are from older deployment methods. Do not use them:

| File | Reason |
|------|--------|
| `deploy.sh` | Old EC2 deployment script |
| `deploy_lambda.py` | Old Lambda CloudFormation |
| `deploy_fargate.py` | Old Fargate deployment script |
| `deploy_to_lambda.py` | Old Lambda script |
| `lambda_deploy.yaml` | Old CloudFormation template |
| `fargate_deploy.yaml` | Old CloudFormation template |
| `setup-ssm-secrets.sh` | Secrets now created by Terraform |
| `cleanup_slack.py` | Legacy script |
| `validate_yaml.py` | Legacy validation |
| `upper_naming.py` | Legacy utility |
| `lambda_handler.py` | Old Lambda handler |

**All deployment is now handled by:**
1. `bitbucket-pipelines.yml` (automated CI/CD)
2. `terraform/` (infrastructure as code)
