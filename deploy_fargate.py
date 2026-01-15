#!/usr/bin/env python3
import boto3
import subprocess
import sys
import os
from pathlib import Path

def get_account_id(sts_client):
    """Get AWS account ID"""
    return sts_client.get_caller_identity()['Account']

def create_ecr_repository(ecr_client, repo_name='solara-etl'):
    """Create ECR repository if it doesn't exist"""
    try:
        repo = ecr_client.describe_repositories(repositoryNames=[repo_name])
        print(f"ECR repository '{repo_name}' already exists")
        return repo['repositories'][0]['repositoryUri']
    except ecr_client.exceptions.RepositoryNotFoundException:
        print(f"Creating ECR repository '{repo_name}'...")
        response = ecr_client.create_repository(repositoryName=repo_name)
        return response['repository']['repositoryUri']

def build_and_push_image(ecr_client, repo_uri, region='af-south-1'):
    """Build Docker image and push to ECR"""
    account_id = repo_uri.split('.')[0]
    print(f"\n=== Building Docker Image ===")
    
    tag = 'latest'
    full_image = f"{repo_uri}:{tag}"
    
    print(f"Building image: {full_image}")
    result = subprocess.run(['docker', 'build', '-t', full_image, '.'], cwd='.')
    if result.returncode != 0:
        raise RuntimeError("Docker build failed")
    
    print(f"\nLogging in to ECR...")
    auth_token = subprocess.run(
        ['aws', 'ecr', 'get-login-password', '--region', region],
        capture_output=True,
        text=True
    )
    if auth_token.returncode != 0:
        raise RuntimeError("Failed to get ECR login token")
    
    login_result = subprocess.run(
        ['docker', 'login', '--username', 'AWS', '--password-stdin', f"{account_id}.dkr.ecr.{region}.amazonaws.com"],
        input=auth_token.stdout,
        text=True
    )
    if login_result.returncode != 0:
        raise RuntimeError("Docker login failed")
    
    print(f"Pushing image to ECR...")
    push_result = subprocess.run(['docker', 'push', full_image])
    if push_result.returncode != 0:
        raise RuntimeError("Docker push failed")
    
    print(f"✓ Image pushed: {full_image}")
    return full_image

def set_ssm_parameter(ssm_client, param_name, param_value, region='af-south-1'):
    """Set or update SSM parameter"""
    try:
        ssm_client.put_parameter(
            Name=param_name,
            Value=param_value,
            Type='SecureString',
            Overwrite=True,
            Tier='Standard'
        )
        print(f"✓ SSM parameter set: {param_name}")
    except Exception as e:
        print(f"✗ Failed to set SSM parameter {param_name}: {e}")
        raise

def deploy_cloudformation(cf_client, stack_name, template_file, parameters, region='af-south-1'):
    """Deploy or update CloudFormation stack"""
    print(f"\n=== Deploying CloudFormation Stack ===")
    
    with open(template_file, 'r') as f:
        template_body = f.read()
    
    params = [{'ParameterKey': k, 'ParameterValue': str(v)} for k, v in parameters.items()]
    
    try:
        stacks = cf_client.describe_stacks(StackName=stack_name)['Stacks']
        if stacks:
            status = stacks[0]['StackStatus']
            print(f"Stack exists with status: {status}")
            
            if status in ['ROLLBACK_COMPLETE', 'CREATE_FAILED', 'DELETE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE']:
                print(f"Deleting failed stack...")
                cf_client.delete_stack(StackName=stack_name)
                waiter = cf_client.get_waiter('stack_delete_complete')
                waiter.wait(StackName=stack_name)
            
            print(f"Updating stack...")
            response = cf_client.update_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=params,
                Capabilities=['CAPABILITY_NAMED_IAM']
            )
            print(f"✓ Update initiated: {response['StackId']}")
    except cf_client.exceptions.ClientError as e:
        if 'does not exist' in str(e):
            print(f"Creating new stack...")
            response = cf_client.create_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=params,
                Capabilities=['CAPABILITY_NAMED_IAM']
            )
            print(f"✓ Create initiated: {response['StackId']}")
        else:
            raise

def get_vpc_and_subnets(ec2_client):
    """Get default VPC and subnets"""
    print(f"\n=== Detecting VPC and Subnets ===")
    
    vpcs = ec2_client.describe_vpcs(Filters=[{'Name': 'isDefault', 'Values': ['true']}])['Vpcs']
    if not vpcs:
        raise RuntimeError("No default VPC found. Please specify VPC and Subnets manually.")
    
    vpc_id = vpcs[0]['VpcId']
    print(f"Using VPC: {vpc_id}")
    
    subnets = ec2_client.describe_subnets(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}])['Subnets']
    subnet_ids = [s['SubnetId'] for s in subnets[:2]]
    
    if len(subnet_ids) < 2:
        raise RuntimeError("Need at least 2 subnets. Please check your VPC configuration.")
    
    print(f"Using subnets: {subnet_ids}")
    return vpc_id, subnet_ids

def main():
    region = os.getenv('AWS_REGION', 'af-south-1')
    
    print(f"AWS Region: {region}")
    print(f"Working directory: {os.getcwd()}")
    
    boto3.setup_default_session(region_name=region)
    sts = boto3.client('sts')
    ecr = boto3.client('ecr')
    ssm = boto3.client('ssm')
    cf = boto3.client('cloudformation')
    ec2 = boto3.client('ec2')
    
    account_id = get_account_id(sts)
    print(f"Account ID: {account_id}")
    
    repo_uri = create_ecr_repository(ecr)
    image_uri = build_and_push_image(ecr, repo_uri, region)
    
    print(f"\n=== Setting up Secrets ===")
    db_password = os.getenv('DB_PASSWORD')
    slack_token = os.getenv('SLACK_BOT_TOKEN')
    
    if not db_password:
        raise ValueError("DB_PASSWORD environment variable not set")
    if not slack_token:
        raise ValueError("SLACK_BOT_TOKEN environment variable not set")
    
    set_ssm_parameter(ssm, '/solara-etl/db-password', db_password, region)
    set_ssm_parameter(ssm, '/solara-etl/slack-bot-token', slack_token, region)
    
    vpc_id, subnet_ids = get_vpc_and_subnets(ec2)
    
    stack_name = 'solara-etl-fargate'
    parameters = {
        'ECRImageUri': image_uri,
        'DBHost': os.getenv('DB_HOST', 'solara.crqioqgga31u.af-south-1.rds.amazonaws.com'),
        'DBPort': os.getenv('DB_PORT', '5432'),
        'DBName': os.getenv('DB_NAME', 'solara'),
        'DBUser': os.getenv('DB_USER', 'tableau'),
        'DBPassword': db_password,
        'SlackBotToken': slack_token,
        'SlackChannelId': os.getenv('SLACK_CHANNEL_ID'),
        'VpcId': vpc_id,
        'SubnetIds': ','.join(subnet_ids),
    }
    
    deploy_cloudformation(cf, stack_name, 'fargate_deploy.yaml', parameters, region)
    
    print(f"\n=== Deployment Complete ===")
    print(f"Stack name: {stack_name}")
    print(f"Image: {image_uri}")
    print(f"CloudWatch Logs: /aws/fargate/solara-etl")
    print(f"\nTo monitor logs:")
    print(f"  aws logs tail /aws/fargate/solara-etl --follow --region {region}")

if __name__ == '__main__':
    main()
