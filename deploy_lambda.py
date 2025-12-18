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

def validate_template(template_file, region='af-south-1'):
    cf = boto3.client('cloudformation', region_name=region)
    
    with open(template_file, 'r') as f:
        template_body = f.read()
    
    try:
        result = cf.validate_template(TemplateBody=template_body)
        print("✓ Template validation passed")
        print(f"  Parameters: {len(result.get('Parameters', []))}")
        print(f"  Resources: {len(result.get('Capabilities', []))}")
        return True
    except cf.exceptions.ClientError as e:
        print(f"✗ Template validation failed: {e}")
        return False

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

def create_change_set_preview(stack_name, template_file, parameters=None, region='af-south-1'):
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
        change_set_type = 'UPDATE'
    except cf.exceptions.ClientError as e:
        if 'does not exist' in str(e):
            change_set_type = 'CREATE'
        else:
            raise
    
    change_set_name = f'{stack_name}-preview-{int(__import__("time").time())}'
    
    try:
        print(f"Creating change set ({change_set_type})...")
        cf.create_change_set(
            StackName=stack_name,
            ChangeSetName=change_set_name,
            TemplateBody=template_body,
            Parameters=params,
            Capabilities=['CAPABILITY_NAMED_IAM'],
            ChangeSetType=change_set_type
        )
        
        waiter = cf.get_waiter('change_set_create_complete')
        waiter.wait(StackName=stack_name, ChangeSetName=change_set_name)
        
        changes = cf.describe_change_set(StackName=stack_name, ChangeSetName=change_set_name)
        
        if 'Changes' not in changes or not changes['Changes']:
            print("✓ No changes detected")
        else:
            print(f"✓ {len(changes['Changes'])} changes will be applied:")
            for change in changes['Changes']:
                action = change['Type']
                resource = change['ResourceChange']['LogicalResourceId']
                resource_type = change['ResourceChange']['ResourceType']
                print(f"  - {action}: {resource} ({resource_type})")
        
        print("\n✓ Change set preview successful - no errors detected")
        
        cf.delete_change_set(StackName=stack_name, ChangeSetName=change_set_name)
        return True
    except cf.exceptions.ClientError as e:
        print(f"\n✗ Change set failed: {e}")
        try:
            cf.delete_change_set(StackName=stack_name, ChangeSetName=change_set_name)
        except:
            pass
        return False

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
        print("Step 0: Validating CloudFormation template...")
        if not validate_template(template_file, region):
            sys.exit(1)
        
        print("\nStep 1: Getting EC2 instance ID...")
        ec2_instance = get_ec2_instance_id(region)
        
        print("\nStep 2: Deploying CloudFormation stack...")
        parameters = {
            'EC2InstanceId': ec2_instance,
            'EventBridgeRuleName': 'solara-etl-schedule',
            'ScheduleExpression': 'cron(0 */2 * * ? *)',
            'CloudWatchLogGroup': '/aws/ssm/solara-etl',
            'ScriptPath': '/home/ec2-user/etl_project/main.py',
        }
        deploy_cloudformation(stack_name, template_file, parameters, region)
        
        print("\nDeployment complete!")
        print("\nStack outputs:")
        cf = boto3.client('cloudformation', region_name=region)
        stacks = cf.describe_stacks(StackName=stack_name)['Stacks']
        if stacks:
            for output in stacks[0].get('Outputs', []):
                print(f"  {output['OutputKey']}: {output['OutputValue']}")
    elif action == 'validate':
        print("Step 1: Syntax validation...")
        if not validate_template(template_file, region):
            sys.exit(1)
        
        print("\nStep 2: Getting EC2 instance ID...")
        try:
            ec2_instance = get_ec2_instance_id(region)
        except ValueError as e:
            print(f"✗ {e}")
            sys.exit(1)
        
        print("\nStep 3: Preview deployment changes (change set)...")
        parameters = {
            'EC2InstanceId': ec2_instance,
            'EventBridgeRuleName': 'solara-etl-schedule',
            'ScheduleExpression': 'cron(0 */2 * * ? *)',
            'CloudWatchLogGroup': '/aws/ssm/solara-etl',
            'ScriptPath': '/home/ec2-user/etl_project/main.py',
        }
        
        stack_name = 'solara-etl-stack'
        if create_change_set_preview(stack_name, template_file, parameters, region):
            print("\n✓ All validations passed! Pipeline is safe to deploy.")
            sys.exit(0)
        else:
            sys.exit(1)
    else:
        print(f"Unknown action: {action}")
        print("Usage: python deploy_lambda.py [deploy|validate]")

if __name__ == '__main__':
    main()
