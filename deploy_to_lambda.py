#!/usr/bin/env python3
import boto3
import zipfile
import os
import subprocess
import sys
from pathlib import Path

def create_deployment_package(output_zip='lambda_function.zip'):
    print(f"Creating deployment package: {output_zip}")
    
    with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipf.write('lambda_handler.py')
        zipf.write('main.py')
        
        if os.path.exists('logs'):
            for root, dirs, files in os.walk('logs'):
                for file in files:
                    file_path = os.path.join(root, file)
                    zipf.write(file_path)

def install_dependencies(output_zip='lambda_function.zip'):
    print("Installing dependencies...")
    
    deps_dir = 'python_deps'
    if os.path.exists(deps_dir):
        subprocess.run(['rm', '-rf', deps_dir], check=True)
    
    os.makedirs(deps_dir)
    subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt', '-t', deps_dir],
        check=True
    )
    
    with zipfile.ZipFile(output_zip, 'a') as zipf:
        for root, dirs, files in os.walk(deps_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = file_path.replace(deps_dir + os.sep, '')
                if not file_path.endswith('.pyc'):
                    zipf.write(file_path, arcname)
    
    subprocess.run(['rm', '-rf', deps_dir], check=True)
    print(f"Deployment package ready: {output_zip}")

def deploy_to_lambda(function_name='solara-etl', zip_file='lambda_function.zip', region='af-south-1'):
    print(f"Deploying to Lambda function: {function_name}")
    
    lambda_client = boto3.client('lambda', region_name=region)
    iam_client = boto3.client('iam', region_name=region)
    
    role_name = f'{function_name}-role'
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "lambda.amazonaws.com"
                },
                "Action": "sts:AssumeRole"
            }
        ]
    }
    
    try:
        role = iam_client.get_role(RoleName=role_name)
        role_arn = role['Role']['Arn']
        print(f"Using existing role: {role_arn}")
    except iam_client.exceptions.NoSuchEntityException:
        print(f"Creating IAM role: {role_name}")
        role = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=str(assume_role_policy).replace("'", '"'),
            Description='Role for Solara ETL Lambda function'
        )
        role_arn = role['Role']['Arn']
        
        policies = [
            'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole',
            'arn:aws:iam::aws:policy/CloudWatchLogsFullAccess',
            'arn:aws:iam::aws:policy/AmazonSSMFullAccess'
        ]
        for policy in policies:
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy)
        
        print("Role created and policies attached")
        import time
        time.sleep(10)
    
    with open(zip_file, 'rb') as f:
        zip_content = f.read()
    
    try:
        lambda_client.get_function(FunctionName=function_name)
        print(f"Updating existing function: {function_name}")
        response = lambda_client.update_function_code(
            FunctionName=function_name,
            ZipFile=zip_content
        )
    except lambda_client.exceptions.ResourceNotFoundException:
        print(f"Creating new function: {function_name}")
        response = lambda_client.create_function(
            FunctionName=function_name,
            Runtime='python3.12',
            Role=role_arn,
            Handler='lambda_handler.lambda_handler',
            Code={'ZipFile': zip_content},
            Timeout=900,
            MemorySize=512,
            Description='Solara ETL Pipeline'
        )
    
    print(f"Lambda function deployed: {response['FunctionArn']}")
    return response['FunctionArn']

def set_environment_variables(function_name, env_vars, region='af-south-1'):
    print("Setting environment variables...")
    lambda_client = boto3.client('lambda', region_name=region)
    
    lambda_client.update_function_configuration(
        FunctionName=function_name,
        Environment={'Variables': env_vars}
    )
    print("Environment variables set")

def create_eventbridge_trigger(function_name, function_arn, schedule='cron(0 */2 * * ? *)', region='af-south-1'):
    print(f"Creating EventBridge trigger for schedule: {schedule}")
    
    events_client = boto3.client('events', region_name=region)
    lambda_client = boto3.client('lambda', region_name=region)
    
    rule_name = f'{function_name}-schedule'
    
    try:
        events_client.put_rule(
            Name=rule_name,
            ScheduleExpression=schedule,
            State='ENABLED',
            Description=f'Schedule for {function_name}'
        )
        print(f"EventBridge rule created: {rule_name}")
    except Exception as e:
        print(f"EventBridge rule exists or error: {e}")
    
    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId=f'{rule_name}-permission',
            Action='lambda:InvokeFunction',
            Principal='events.amazonaws.com',
            SourceArn=f'arn:aws:events:{region}:' + boto3.client('sts').get_caller_identity()['Account'] + f':rule/{rule_name}'
        )
    except lambda_client.exceptions.ResourceConflictException:
        print("Lambda permission already exists")
    
    events_client.put_targets(
        Rule=rule_name,
        Targets=[
            {
                'Id': '1',
                'Arn': function_arn,
                'RoleArn': f'arn:aws:iam::' + boto3.client('sts').get_caller_identity()['Account'] + ':role/service-role/EventBridgeLambdaRole'
            }
        ]
    )
    print(f"EventBridge trigger created for Lambda")

def main():
    import dotenv
    
    dotenv.load_dotenv()
    
    function_name = 'solara-etl'
    region = os.getenv('AWS_REGION', 'af-south-1')
    zip_file = 'lambda_function.zip'
    
    env_vars = {
        'DB_HOST': os.getenv('DB_HOST', ''),
        'DB_PORT': os.getenv('DB_PORT', '5432'),
        'DB_NAME': os.getenv('DB_NAME', ''),
        'DB_USER': os.getenv('DB_USER', ''),
        'DB_PASSWORD': os.getenv('DB_PASSWORD', ''),
        'DB_SSLMODE': os.getenv('DB_SSLMODE', 'require'),
        'SLACK_BOT_TOKEN': os.getenv('SLACK_BOT_TOKEN', ''),
        'SLACK_CHANNEL_ID': os.getenv('SLACK_CHANNEL_ID', ''),
        'CLOUDWATCH_LOG_GROUP': os.getenv('CLOUDWATCH_LOG_GROUP', 'solara-etl-ec2'),
        'CLOUDWATCH_LOG_STREAM': os.getenv('CLOUDWATCH_LOG_STREAM', 'production-run'),
        'AWS_REGION': region,
    }
    
    print("Step 1: Creating deployment package")
    create_deployment_package(zip_file)
    
    print("\nStep 2: Installing dependencies")
    install_dependencies(zip_file)
    
    print("\nStep 3: Deploying to Lambda")
    function_arn = deploy_to_lambda(function_name, zip_file, region)
    
    print("\nStep 4: Setting environment variables")
    set_environment_variables(function_name, env_vars, region)
    
    print("\nStep 5: Creating EventBridge trigger")
    try:
        create_eventbridge_trigger(function_name, function_arn, region=region)
    except Exception as e:
        print(f"EventBridge trigger setup skipped (may need manual role creation): {e}")
    
    print("\nDeployment complete!")
    print(f"Function: {function_name}")
    print(f"Region: {region}")
    print(f"Schedule: cron(0 */2 * * ? *) - Every 2 hours")
    print("\nTo change schedule, update the EventBridge rule in AWS Console")

if __name__ == '__main__':
    main()
