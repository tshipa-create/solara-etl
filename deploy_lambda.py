#!/usr/bin/env python3
import boto3
import os
import sys
import subprocess

def get_ec2_instance_id(region):
    ec2_instance = os.getenv('EC2_INSTANCE_ID')
    
    if ec2_instance:
        print(f"Using EC2 instance from environment: {ec2_instance}")
        return ec2_instance
    
    print("EC2_INSTANCE_ID not set. Fetching from EC2 metadata...")
    try:
        result = subprocess.run(
            ['curl', '-s', 'http://169.254.169.254/latest/meta-data/instance-id'],
            capture_output=True,
            text=True,
            timeout=2
        )
        if result.returncode == 0 and result.stdout:
            ec2_instance = result.stdout.strip()
            print(f"Auto-detected EC2 instance: {ec2_instance}")
            return ec2_instance
    except Exception:
        pass
    
    raise ValueError("Could not determine EC2_INSTANCE_ID. Set EC2_INSTANCE_ID environment variable or run on EC2")

def get_stack_events(cf, stack_name):
    try:
        events = cf.describe_stack_events(StackName=stack_name)['StackEvents']
        print("\nStack Events:")
        for event in reversed(events):
            status = event['ResourceStatus']
            reason = event.get('ResourceStatusReason', '')
            resource = event['LogicalResourceId']
            print(f"  {resource}: {status}")
            if reason:
                print(f"    Reason: {reason}")
    except Exception as e:
        print(f"Could not retrieve stack events: {e}")

def deploy_cloudformation(stack_name, template_file, parameters=None, region='af-south-1'):
    cf = boto3.client('cloudformation', region_name=region)
    
    with open(template_file, 'r') as f:
        template_body = f.read()
    
    params = []
    if parameters:
        for key, value in parameters.items():
            params.append({
                'ParameterKey': key,
                'ParameterValue': str(value)
            })
    
    try:
        stack = cf.describe_stacks(StackName=stack_name)['Stacks'][0]
        status = stack['StackStatus']
        
        if status in ['ROLLBACK_COMPLETE', 'CREATE_FAILED', 'DELETE_COMPLETE', 'UPDATE_ROLLBACK_COMPLETE']:
            print(f"Stack {stack_name} is in {status} state. Deleting and recreating...")
            cf.delete_stack(StackName=stack_name)
            waiter = cf.get_waiter('stack_delete_complete')
            waiter.wait(StackName=stack_name)
            print(f"Stack {stack_name} deleted. Creating new stack...")
            response = cf.create_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=params,
                Capabilities=['CAPABILITY_NAMED_IAM']
            )
            print(f"Create initiated: {response['StackId']}")
        else:
            print(f"Stack {stack_name} exists with status {status}, updating...")
            response = cf.update_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=params,
                Capabilities=['CAPABILITY_NAMED_IAM']
            )
            print(f"Update initiated: {response['StackId']}")
    except cf.exceptions.ClientError as e:
        if 'does not exist' in str(e):
            print(f"Stack {stack_name} doesn't exist, creating...")
            response = cf.create_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=params,
                Capabilities=['CAPABILITY_NAMED_IAM']
            )
            print(f"Create initiated: {response['StackId']}")
        else:
            raise
    
    get_stack_events(cf, stack_name)

def main():
    region = os.getenv('AWS_REGION', 'af-south-1')
    stack_name = 'solara-etl-stack'
    template_file = 'lambda_deploy.yaml'
    
    if len(sys.argv) > 1:
        action = sys.argv[1]
    else:
        action = 'deploy'
    
    if action == 'deploy':
        print("Step 1: Getting EC2 instance ID...")
        ec2_instance = get_ec2_instance_id(region)
        
        print("\nStep 2: Deploying CloudFormation stack...")
        parameters = {
            'EC2InstanceId': ec2_instance,
            'EventBridgeRuleName': 'solara-etl-schedule',
            'ScheduleExpression': 'cron(0 */2 * * ? *)',
            'LogGroupNameParam': '/aws/ssm/solara-etl',
        }
        deploy_cloudformation(stack_name, template_file, parameters, region)
        
        print("\nDeployment complete!")
        print("\nStack outputs:")
        cf = boto3.client('cloudformation', region_name=region)
        stacks = cf.describe_stacks(StackName=stack_name)['Stacks']
        if stacks:
            for output in stacks[0].get('Outputs', []):
                print(f"  {output['OutputKey']}: {output['OutputValue']}")
    else:
        print(f"Unknown action: {action}")
        print("Usage: python deploy_lambda.py deploy")

if __name__ == '__main__':
    main()
