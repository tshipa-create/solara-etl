# Solara ETL Pipeline Deployment Guide

## Architecture Overview

The Solara ETL pipeline modernizes scheduling and logging on AWS:

- **Scheduling**: AWS EventBridge (cron-based, every 2 hours)
- **Execution**: EC2 instance running Python script via SSM Run Command
- **Logging**: CloudWatch Logs (centralized, 30-day retention)
- **Source**: PostgreSQL (on RDS)
- **Target**: Snowflake Data Warehouse

## Prerequisites

### 1. AWS Setup

#### IAM Role for EC2 Instance
- EC2 instance must have `AmazonSSMManagedInstanceCore` policy attached
- This allows EventBridge to send commands to the instance via Systems Manager

#### SSM Parameters (us-east-1 region)
- `/snowflake/connection_private_key` - Private key (unencrypted or passphrase-protected)
- `/snowflake/connection_passphrase` - Passphrase for private key (optional, leave empty if no passphrase)

#### SSM Parameters (af-south-1 region)
- `/odoo_etl/SNOWFLAKE_ACCOUNT` - Snowflake account identifier
- `/odoo_etl/SNOWFLAKE_USER` - Snowflake username
- `/odoo_etl/SNOWFLAKE_DATABASE` - Snowflake database name
- `/odoo_etl/SNOWFLAKE_WAREHOUSE` - Snowflake warehouse name

### 2. EC2 Instance Setup

#### System Requirements
- Python 3.8+
- `curl` installed (for metadata fetch in deployment script)
- SSM Agent running (default on Amazon Linux 2)

#### Environment Setup

1. **Create project directory**
```bash
mkdir -p /home/ec2-user/etl_project
cd /home/ec2-user/etl_project
```

2. **Create Python virtual environment**
```bash
python3 -m venv venv
source venv/bin/activate
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Create .env file**
```bash
cat > .env << EOF
DB_HOST=your-rds-host.af-south-1.rds.amazonaws.com
DB_PORT=5432
DB_NAME=solara
DB_USER=your_db_user
DB_PASSWORD=your_db_password
CLOUDWATCH_LOG_GROUP=/aws/ssm/solara-etl
CLOUDWATCH_LOG_STREAM=etl-pipeline
EOF
```

### 3. Bitbucket Repository Variables

Set these as repository variables (Settings → Repository Variables):

| Variable | Example Value | Description |
|----------|---------------|-------------|
| `AWS_ACCESS_KEY_ID` | `AKIA...` | AWS IAM user access key |
| `AWS_SECRET_ACCESS_KEY` | `...` | AWS IAM user secret key |
| `EC2_HOST` | `ec2-user@10.0.1.100` | EC2 instance SSH address |
| `EC2_INSTANCE_ID` | `i-0123456789abcdef0` | EC2 instance ID |
| `DB_PASSWORD` | `...` | PostgreSQL password |

## Deployment Methods

### Method 1: Automated via Bitbucket Pipeline (Recommended)

The pipeline automatically:
1. Syncs code to EC2 via rsync
2. Detects EC2 instance ID
3. Creates CloudFormation stack with EventBridge rule
4. Configures SSM execution permissions
5. Sets up CloudWatch log group

**To deploy:**
Push code to repository:
```bash
git push origin main
```

The pipeline runs automatically and deploys the infrastructure.

### Method 2: Manual Deployment

**Prerequisites:**
- AWS credentials configured locally (`~/.aws/credentials`)
- All code synced to EC2

**Steps:**

1. **Install Python dependencies**
```bash
pip install boto3
```

2. **Run deployment script**
```bash
export AWS_REGION=af-south-1
export EC2_INSTANCE_ID=i-0123456789abcdef0
python deploy_lambda.py deploy
```

The script will:
- Verify EC2 instance exists
- Create CloudFormation stack (or update if exists)
- Configure EventBridge rule
- Set up IAM roles and CloudWatch logs

## Verification Steps

### 1. CloudFormation Stack
```bash
aws cloudformation describe-stacks \
  --stack-name solara-etl-stack \
  --region af-south-1 \
  --output table
```

### 2. EventBridge Rule
```bash
aws events describe-rule \
  --name solara-etl-schedule \
  --region af-south-1
```

### 3. Manual Test - Trigger SSM Command
```bash
EC2_ID=i-0123456789abcdef0
aws ssm send-command \
  --instance-ids $EC2_ID \
  --document-name "AWS-RunShellScript" \
  --parameters 'command=["cd /home/ec2-user/etl_project && source venv/bin/activate && python main.py"]' \
  --region af-south-1
```

Monitor the command:
```bash
aws ssm get-command-invocation \
  --command-id <command-id-from-above> \
  --instance-id $EC2_ID \
  --region af-south-1
```

### 4. CloudWatch Logs
```bash
aws logs tail /aws/ssm/solara-etl --follow --region af-south-1
```

## Pipeline Execution

### Automatic (EventBridge)
- Runs every 2 hours (configurable via `ScheduleExpression` in template)
- Logs to `/aws/ssm/solara-etl` CloudWatch log group
- Failures are retried (max 2 retries, 1 hour between retries)

### Manual
```bash
cd /home/ec2-user/etl_project
source venv/bin/activate
python main.py
```

## Data Validation

Run the validation script to compare record counts:
```bash
python validate_record_counts.py
```

**Output includes:**
- Total record counts per table in PostgreSQL
- Comparison with Snowflake (if connection successful)
- Identifies tables with mismatches

## Troubleshooting

### Pipeline Not Running
1. Check EventBridge rule is `ENABLED`
```bash
aws events describe-rule --name solara-etl-schedule --region af-south-1
```

2. Verify EC2 instance is in target group and has SSM Agent running
```bash
aws ssm describe-instance-information --instance-information-filter-list "key=InstanceIds,valueSet=i-0123456789abcdef0" --region af-south-1
```

### Snowflake Connection Errors

**"Password is empty" error:**
- Ensure `/snowflake/connection_passphrase` is set (can be empty string if no passphrase)
- Script now handles both password-protected and unencrypted keys

**Missing credentials:**
```bash
aws ssm get-parameter --name /odoo_etl/SNOWFLAKE_ACCOUNT --region af-south-1 --with-decryption
```

### CloudWatch Logs Missing
- Check SSM role has `logs:PutLogEvents` permission
- Verify log group exists: `/aws/ssm/solara-etl`
- Check SSM command output config is enabled in EventBridge target

### PostgreSQL Connection Issues
- Verify `.env` has correct DB credentials
- Test connection: `psql -h $DB_HOST -U $DB_USER -d $DB_NAME`
- Check RDS security group allows EC2 inbound on port 5432

## Configuration

### EventBridge Schedule
Edit `lambda_deploy.yaml`, update `ScheduleExpression` parameter:

| Frequency | Cron Expression |
|-----------|-----------------|
| Every hour | `cron(0 * * * ? *)` |
| Every 2 hours | `cron(0 */2 * * ? *)` |
| Daily at 2 AM | `cron(0 2 * * ? *)` |
| Every 6 hours | `cron(0 */6 * * ? *)` |

Then redeploy:
```bash
python deploy_lambda.py deploy
```

### CloudWatch Log Retention
Edit `lambda_deploy.yaml`, update `CloudWatchLogGroup` resource:
```yaml
RetentionInDays: 30  # Change to desired days
```

## Files Overview

| File | Purpose |
|------|---------|
| `main.py` | ETL pipeline logic (dlt-based) |
| `lambda_deploy.yaml` | CloudFormation template for EventBridge + SSM + CloudWatch |
| `deploy_lambda.py` | Automation script for CloudFormation deployment |
| `bitbucket-pipelines.yml` | CI/CD pipeline configuration |
| `requirements.txt` | Python dependencies |
| `validate_record_counts.py` | Data quality validation tool |
| `upper_naming.py` | Schema naming convention utility |

## Database Credentials & Secrets Management

### Local Development (.env)
Never commit `.env` file with real credentials.

### Production (AWS SSM Parameter Store)
- Snowflake credentials stored in us-east-1 SSM (key material region)
- Database credentials in Bitbucket repository variables (encrypted)
- EC2 instance retrieves credentials at runtime

## Support & Monitoring

### Daily Operations
- Check CloudWatch logs: `/aws/ssm/solara-etl`
- Monitor failed EventBridge invocations (CloudWatch Events metrics)
- Validate record counts weekly: `python validate_record_counts.py`

### Escalation
If pipeline fails:
1. Check CloudWatch logs for error messages
2. Verify all SSM parameters are correctly set
3. Test manual SSM command execution
4. Check PostgreSQL and Snowflake connectivity independently
