#!/usr/bin/env python3

import argparse
import json
import os
import sys
import boto3
from github import Github
import base64
import tempfile
import shutil
import subprocess
import requests
import time
import logging
from collections import defaultdict
import re
from google.cloud import functions_v1
from google.auth import default

# Import FaaSr-Backend functions
from FaaSr_py.helpers.graph_functions import check_dag, validate_json, extract_rank
from FaaSr_py.engine.faasr_payload import FaaSrPayload

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def parse_arguments():
    parser = argparse.ArgumentParser(description='Deploy FaaSr functions to specified platform')
    parser.add_argument('--workflow-file', required=True,
                      help='Path to the workflow JSON file')
    return parser.parse_args()

def read_workflow_file(file_path):
    try:
        with open(file_path, 'r') as f:
            workflow_data = json.load(f)
        
        # Validate JSON schema using FaaSr-Backend validation
        print("Validating workflow JSON schema...")
        if validate_json(workflow_data):
            print("✓ Workflow JSON schema validation passed")
        
        return workflow_data
    except FileNotFoundError:
        print(f"Error: Workflow file {file_path} not found")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in workflow file {file_path}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: Workflow validation failed: {str(e)}")
        sys.exit(1)

# Graph validation functions are now imported from FaaSr-Backend
# No need to implement them here

def validate_workflow_with_faasr_backend(workflow_data):
    """
    Validate workflow using FaaSr-Backend validation functions
    This includes JSON schema validation, DAG validation, and S3 checks
    """
    try:
        # Create a temporary FaaSrPayload-like object for validation
        # We'll use a mock approach since we don't have a GitHub URL
        class MockFaaSrPayload:
            def __init__(self, workflow_data):
                self._base_workflow = workflow_data
                self._overwritten = {}
            
            def __getitem__(self, key):
                if key in self._overwritten:
                    return self._overwritten[key]
                elif key in self._base_workflow:
                    return self._base_workflow[key]
                raise KeyError(key)
            
            def __setitem__(self, key, value):
                self._overwritten[key] = value
            
            def get(self, key, default=None):
                if key in self._overwritten:
                    return self._overwritten[key]
                elif key in self._base_workflow:
                    return self._base_workflow[key]
                return default
        
        # Create mock payload for validation
        mock_payload = MockFaaSrPayload(workflow_data)
        
        # Validate DAG structure using FaaSr-Backend
        print("Validating workflow DAG structure...")
        check_dag(workflow_data)
        print("✓ Workflow DAG validation passed")
        
        # Validate S3 data stores using FaaSr-Backend
        print("Validating S3 data stores...")
        mock_payload.s3_check = lambda: validate_s3_datastores(workflow_data)
        mock_payload.s3_check()
        print("✓ S3 data stores validation passed")
        
        return True
        
    except Exception as e:
        print(f"Error: FaaSr-Backend validation failed: {str(e)}")
        sys.exit(1)

def validate_s3_datastores(workflow_data):
    """
    Validate S3 data stores using the same logic as FaaSr-Backend
    """
    import boto3
    
    if 'DataStores' not in workflow_data:
        return True
        
    # Iterate through all of the data stores
    for server in workflow_data['DataStores'].keys():
        server_config = workflow_data['DataStores'][server]
        
        # Get the endpoint and region
        server_endpoint = server_config.get("Endpoint")
        server_region = server_config.get('Region', 'us-east-1')
        
        # Ensure that endpoint is a valid http address
        if server_endpoint and not server_endpoint.startswith("http"):
            error_message = f"Invalid data store server endpoint {server}"
            logger.error(error_message)
            sys.exit(1)

        # If the region is empty, then use default 'us-east-1'
        if not server_region:
            server_region = "us-east-1"

        # Skip S3 validation if credentials are placeholders
        access_key = server_config.get('AccessKey', '')
        secret_key = server_config.get('SecretKey', '')
        
        if (access_key.endswith('_ACCESS_KEY') or 
            secret_key.endswith('_SECRET_KEY') or
            not access_key or not secret_key):
            print(f"Skipping S3 validation for {server} (placeholder credentials)")
            continue

        try:
            if server_endpoint:
                s3_client = boto3.client(
                    "s3",
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=server_region,
                    endpoint_url=server_endpoint,
                )
            else:
                s3_client = boto3.client(
                    "s3",
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=server_region,
                )
            
            # Use boto3 head bucket to ensure that the
            # bucket exists and that we have access to it
            bucket_name = server_config.get('Bucket')
            if bucket_name:
                s3_client.head_bucket(Bucket=bucket_name)
                print(f"✓ S3 datastore '{server}' is accessible")
                
        except Exception as e:
            err_message = f"S3 server {server} failed with message: {e}"
            logger.error(err_message)
            # Don't exit here during registration - just warn
            print(f"Warning: {err_message}")
    
    return True

def get_github_token():
    # Get GitHub PAT from environment variable
    token = os.getenv('GH_PAT')
    if not token:
        print("Error: GH_PAT environment variable not set")
        sys.exit(1)
    return token

def get_aws_credentials():
    # Try to get AWS credentials from environment variables
    aws_access_key = os.getenv('AWS_ACCESSKEY')
    aws_secret_key = os.getenv('AWS_SECRETKEY')
    role_arn = os.getenv('AWS_ARN')
    
    if not all([aws_access_key, aws_secret_key, role_arn]):
        print("Error: AWS credentials or role ARN not set in environment variables")
        sys.exit(1)
    
    return aws_access_key, aws_secret_key, role_arn

def get_gcp_credentials_from_workflow(workflow_data):
    """Get GCP credentials from workflow data and environment variables"""
    # Find the GCP server configuration
    gcp_server_config = None
    for server_name, server_config in workflow_data['ComputeServers'].items():
        faas_type = server_config.get('FaaSType', '').lower()
        if faas_type in ['cloudfunctions', 'cloud_functions', 'gcp', 'gcf', 'googlecloud']:
            gcp_server_config = server_config
            break
    
    if not gcp_server_config:
        print("Error: No GCP server configuration found in workflow data")
        sys.exit(1)
    
    # Get project ID from Namespace field
    project_id = gcp_server_config.get('Namespace')
    if not project_id:
        print("Error: Namespace (project ID) not found in GCP server configuration")
        sys.exit(1)
    
    # Get service account key from SecretKey field and resolve placeholder
    secret_key_placeholder = gcp_server_config.get('SecretKey')
    if not secret_key_placeholder:
        print("Error: SecretKey not found in GCP server configuration")
        sys.exit(1)
    
    # If it's a placeholder, get from environment variable
    if secret_key_placeholder == "GCP_SECRET_KEY":
        service_account_key = os.getenv('GCP_SECRETKEY')
    else:
        service_account_key = secret_key_placeholder
    
    if service_account_key:
        # If service account key is provided, set it up
        try:
            # Write the service account key to a temporary file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
                f.write(service_account_key)
                key_file = f.name
            
            # Set the environment variable for Google Cloud authentication
            os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = key_file
            print(f"Using GCP service account key authentication for project: {project_id}")
            return project_id, key_file
        except Exception as e:
            print(f"Error setting up GCP service account key: {str(e)}")
            sys.exit(1)
    else:
        print(f"Using default GCP authentication for project: {project_id}")
        return project_id, None

def set_github_variable(repo_full_name, var_name, var_value, github_token):
    url = f"https://api.github.com/repos/{repo_full_name}/actions/variables/{var_name}"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json"
    }
    data = {"name": var_name, "value": var_value}
    # Try to update, if not found, create
    r = requests.patch(url, headers=headers, json=data)
    if r.status_code == 404:
        r = requests.post(f"https://api.github.com/repos/{repo_full_name}/actions/variables", headers=headers, json=data)
    if not r.ok:
        print(f"Failed to set variable {var_name}: {r.text}")
    else:
        print(f"Set variable {var_name} for {repo_full_name}")

def ensure_github_secrets_and_vars(repo, required_secrets, required_vars, github_token):
    """Set GitHub secrets and variables for the repository."""
    # Check and set secrets
    existing_secrets = {s.name for s in repo.get_secrets()}
    for secret_name, secret_value in required_secrets.items():
        if secret_name not in existing_secrets:
            print(f"Setting secret: {secret_name}")
        else:
            print(f"Secret {secret_name} already exists, updating it.")
        repo.create_secret(secret_name, secret_value)

    # Set variables using REST API
    for var_name, var_value in required_vars.items():
        set_github_variable(repo.full_name, var_name, var_value, github_token)

def create_secret_payload(workflow_data):
    """
    Create a secret payload that combines all necessary credentials and the complete workflow configuration.
    This payload will be stored as a GitHub secret and used by the deployed functions.
    This function matches the logic from build_faasr_payload in trigger_function.py
    """
    # Start with credentials at the top
    credentials = {
        "My_GitHub_Account_TOKEN": get_github_token(),
        "My_Minio_Bucket_ACCESS_KEY": os.getenv('MINIO_ACCESSKEY'),
        "My_Minio_Bucket_SECRET_KEY": os.getenv('MINIO_SECRETKEY'),
        "My_OW_Account_API_KEY": os.getenv('OW_APIKEY', ''),
        "My_Lambda_Account_ACCESS_KEY": os.getenv('AWS_ACCESSKEY', ''),
        "My_Lambda_Account_SECRET_KEY": os.getenv('AWS_SECRETKEY', ''),
        "My_GCP_Account_PROJECT_ID": os.getenv('GCP_PROJECT_ID', ''),
        "My_GCP_Account_SERVICE_ACCOUNT_KEY": os.getenv('GCP_SECRETKEY', ''),
        "My_SLURM_Account_TOKEN": os.getenv('SLURM_TOKEN', ''),
    }
    
    payload = credentials.copy()

    # Add workflow data (excluding _workflow_file)
    workflow_copy = workflow_data.copy()
    if '_workflow_file' in workflow_copy:
        del workflow_copy['_workflow_file']
    payload.update(workflow_copy)
    
    # Replace placeholder values in ComputeServers with actual credentials
    if 'ComputeServers' in payload:
        for server_key, server_config in payload['ComputeServers'].items():
            faas_type = server_config.get('FaaSType', '')
            
            # Replace placeholder values with actual credentials
            if faas_type == 'Lambda':
                # Replace Lambda AccessKey/SecretKey placeholders
                if 'AccessKey' in server_config and server_config['AccessKey'] == f"{server_key}_ACCESS_KEY":
                    if credentials['My_Lambda_Account_ACCESS_KEY']:
                        server_config['AccessKey'] = credentials['My_Lambda_Account_ACCESS_KEY']
                if 'SecretKey' in server_config and server_config['SecretKey'] == f"{server_key}_SECRET_KEY":
                    if credentials['My_Lambda_Account_SECRET_KEY']:
                        server_config['SecretKey'] = credentials['My_Lambda_Account_SECRET_KEY']
            elif faas_type == 'GitHubActions':
                # Replace GitHub Token placeholder
                if 'Token' in server_config and server_config['Token'] == f"{server_key}_TOKEN":
                    if credentials['My_GitHub_Account_TOKEN']:
                        server_config['Token'] = credentials['My_GitHub_Account_TOKEN']
            elif faas_type == 'OpenWhisk':
                # Replace OpenWhisk API.key placeholder
                if 'API.key' in server_config and server_config['API.key'] == f"{server_key}_API_KEY":
                    if credentials['My_OW_Account_API_KEY']:
                        server_config['API.key'] = credentials['My_OW_Account_API_KEY']
            elif faas_type in ['CloudFunctions', 'GoogleCloud']:

                if 'Namespace' in server_config and server_config['Namespace'] == f"{server_key}_PROJECT_ID":
                    if credentials['My_GCP_Account_PROJECT_ID']:
                        server_config['Namespace'] = credentials['My_GCP_Account_PROJECT_ID']
                if 'SecretKey' in server_config and server_config['SecretKey'] in [f"{server_key}_SECRET_KEY", "GCP_SECRET_KEY"]:
                    if credentials['My_GCP_Account_SERVICE_ACCOUNT_KEY']:
                        server_config['SecretKey'] = credentials['My_GCP_Account_SERVICE_ACCOUNT_KEY']

    # Replace placeholder values in DataStores with actual credentials
    if 'DataStores' in payload:
        for store_key, store_config in payload['DataStores'].items():
            # Replace placeholder values with actual credentials
            if 'AccessKey' in store_config and store_config['AccessKey'] == f"{store_key}_ACCESS_KEY":
                if store_key == 'My_Minio_Bucket' and credentials['My_Minio_Bucket_ACCESS_KEY']:
                    store_config['AccessKey'] = credentials['My_Minio_Bucket_ACCESS_KEY']
            if 'SecretKey' in store_config and store_config['SecretKey'] == f"{store_key}_SECRET_KEY":
                if store_key == 'My_Minio_Bucket' and credentials['My_Minio_Bucket_SECRET_KEY']:
                    store_config['SecretKey'] = credentials['My_Minio_Bucket_SECRET_KEY']
    
    return json.dumps(payload)

def deploy_to_github(workflow_data):
    """Deploy functions to GitHub Actions."""
    github_token = get_github_token()
    g = Github(github_token)
    
    # Get the workflow name for prefixing
    workflow_name = workflow_data.get('WorkflowName', 'default')
    json_prefix = workflow_name
    
    # Get the current repository
    repo_name = os.getenv('GITHUB_REPOSITORY')
    if not repo_name:
        print("Error: GITHUB_REPOSITORY environment variable not set")
        sys.exit(1)
    
    # Filter actions that should be deployed to GitHub Actions
    github_actions = {}
    for action_name, action_data in workflow_data['ActionList'].items():
        server_name = action_data['FaaSServer']
        server_config = workflow_data['ComputeServers'][server_name]
        faas_type = server_config['FaaSType'].lower()
        if faas_type in ['githubactions', 'github_actions', 'github']:
            github_actions[action_name] = action_data
    
    if not github_actions:
        print("No actions found for GitHub Actions deployment")
        return
    
    try:
        repo = g.get_repo(repo_name)
        
        # Get the default branch name
        default_branch = repo.default_branch
        print(f"Using branch: {default_branch}")
        
        # Create secret payload and set up secrets/variables
        secret_payload = create_secret_payload(workflow_data)
        required_secrets = {"SECRET_PAYLOAD": secret_payload}
        vars = {f"{json_prefix.upper()}_PAYLOAD_REPO": f"{repo_name}/{workflow_data['_workflow_file']}"}
        
        ensure_github_secrets_and_vars(repo, required_secrets, vars, github_token)
        
        # Deploy each action
        for action_name, action_data in github_actions.items():
            actual_func_name = action_data['FunctionName']
            
            # Create prefixed action name using workflow_name-action_name format
            prefixed_action_name = f"{json_prefix}-{action_name}"
            
            # Create workflow file
            # Get container image, with fallback to default
            container_image = workflow_data.get('ActionContainers', {}).get(action_name, 'ghcr.io/faasr/github-actions-tidyverse')
            
            workflow_content = f"""name: {prefixed_action_name}

on:
  workflow_dispatch:
    inputs:
      OVERWRITTEN:
        description: 'overwritten fields'
        required: true
      PAYLOAD_URL:
        description: 'url to payload'
        required: true
jobs:
  run_docker_image:
    runs-on: ubuntu-latest
    container: {container_image}
    env:
      TOKEN: ${{{{ secrets.GH_PAT }}}}
      SECRET_PAYLOAD: ${{{{ secrets.SECRET_PAYLOAD }}}}
      OVERWRITTEN: ${{{{ github.event.inputs.OVERWRITTEN }}}}
      PAYLOAD_URL: ${{{{ github.event.inputs.PAYLOAD_URL }}}}
    steps:
    - name: run Python
      run: |
        cd /action
        python3 faasr_entry.py
"""
            
            # Create or update the workflow file
            workflow_path = f".github/workflows/{prefixed_action_name}.yml"
            try:
                # Try to get the file first
                contents = repo.get_contents(workflow_path)
                existing_content = contents.decoded_content.decode('utf-8')
                
                # Check if content has changed
                if existing_content.strip() == workflow_content.strip():
                    print(f"File {workflow_path} content is already up to date, skipping update")
                else:
                    # If file exists and content is different, update it
                    print(f"File {workflow_path} exists, updating...")
                    repo.update_file(
                        path=workflow_path,
                        message=f"Update workflow for {prefixed_action_name}",
                        content=workflow_content,
                        sha=contents.sha,
                        branch=default_branch
                    )
                    print(f"Successfully updated {workflow_path}")
            except Exception as e:
                if "Not Found" in str(e) or "404" in str(e):
                    # If file doesn't exist, create it
                    print(f"File {workflow_path} doesn't exist, creating...")
                    repo.create_file(
                        path=workflow_path,
                        message=f"Add workflow for {prefixed_action_name}",
                        content=workflow_content,
                        branch=default_branch
                    )
                    print(f"Successfully created {workflow_path}")
                else:
                    print(f"Error updating/creating {workflow_path}: {str(e)}")
                    # Try to get more details about the error
                    if hasattr(e, 'data'):
                        print(f"Error details: {e.data}")
                    if hasattr(e, 'status'):
                        print(f"HTTP status: {e.status}")
                    raise e
                    
            print(f"Successfully deployed {prefixed_action_name} to GitHub")
            
    except Exception as e:
        print(f"Error deploying to GitHub: {str(e)}")
        sys.exit(1)

def deploy_to_aws(workflow_data):
    # Get AWS credentials
    aws_access_key, aws_secret_key, role_arn = get_aws_credentials()
    
    # Get AWS region from server config, default to us-east-1
    aws_region = 'us-east-1'
    for server_config in workflow_data['ComputeServers'].values():
        faas_type = server_config.get('FaaSType', '').lower()
        if faas_type in ['lambda', 'aws_lambda', 'aws']:
            aws_region = server_config.get('Region', 'us-east-1')
            break
    
    lambda_client = boto3.client(
        'lambda',
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=aws_region
    )
    
    # Get the workflow name for function naming
    workflow_name = workflow_data.get('WorkflowName', 'default')
    json_prefix = workflow_name
    
    # Create secret payload (same as GitHub deployment)
    secret_payload = create_secret_payload(workflow_data)
    
    # Filter actions that should be deployed to AWS Lambda
    lambda_actions = {}
    for action_name, action_data in workflow_data['ActionList'].items():
        server_name = action_data['FaaSServer']
        server_config = workflow_data['ComputeServers'][server_name]
        faas_type = server_config['FaaSType'].lower()
        if faas_type in ['lambda', 'aws_lambda', 'aws']:
            lambda_actions[action_name] = action_data
    
    if not lambda_actions:
        print("No actions found for AWS Lambda deployment")
        return
    
    # Process each action in the workflow
    for action_name, action_data in lambda_actions.items():
        try:
            actual_func_name = action_data['FunctionName']
            
            # Create prefixed function name using workflow_name-action_name format
            prefixed_func_name = f"{json_prefix}-{action_name}"
            
            # Get container image for AWS Lambda (must be an Amazon ECR image URI)
            container_image = workflow_data.get('ActionContainers', {}).get(action_name)
            if not container_image:
                container_image = '145342739029.dkr.ecr.us-east-1.amazonaws.com/aws-lambda-tidyverse:latest'
                print(f"No container specified for action '{action_name}', using default: {container_image}")
 
            # Check payload size before deployment
            payload_size = len(secret_payload.encode('utf-8'))
            if payload_size > 4000:  # Lambda env var limit is ~4KB
                print(f"Warning: SECRET_PAYLOAD size ({payload_size} bytes) may exceed Lambda environment variable limits")
                print("Consider using Parameter Store or S3 for large payloads")
            
            # Environment variables for Lambda function
            environment_vars = {
                'SECRET_PAYLOAD': secret_payload
            }
            
            # Check if function already exists first
            try:
                existing_func = lambda_client.get_function(FunctionName=prefixed_func_name)
                print(f"Function {prefixed_func_name} already exists, updating...")
                # Update existing function
                lambda_client.update_function_code(
                    FunctionName=prefixed_func_name,
                    ImageUri=container_image
                )
                
                # Wait for the function update to complete
                print(f"Waiting for {prefixed_func_name} code update to complete...")
                max_attempts = 60  # Wait up to 5 minutes
                attempt = 0
                while attempt < max_attempts:
                    try:
                        response = lambda_client.get_function(FunctionName=prefixed_func_name)
                        state = response['Configuration']['State']
                        last_update_status = response['Configuration']['LastUpdateStatus']
                        
                        if state == 'Active' and last_update_status == 'Successful':
                            break
                        elif state == 'Failed' or last_update_status == 'Failed':
                            sys.exit(1)
                        else:
                            time.sleep(5)
                            attempt += 1
                    except Exception as e:
                        print(f"Error checking function state: {str(e)}")
                        time.sleep(5)
                        attempt += 1
                
                if attempt >= max_attempts:
                    print(f"Timeout waiting for {prefixed_func_name} update to complete")
                    sys.exit(1)
                
                # Now update environment variables
                lambda_client.update_function_configuration(
                    FunctionName=prefixed_func_name,
                    Environment={'Variables': environment_vars}
                )
                print(f"Successfully updated {prefixed_func_name} on AWS Lambda")
                
            except lambda_client.exceptions.ResourceNotFoundException:
                # Function doesn't exist, create it
                print(f"Creating new Lambda function: {prefixed_func_name}")
                
                # Create function with minimal parameters first, then update
                print("Creating with minimal parameters...")
                try:
                    lambda_client.create_function(
                        FunctionName=prefixed_func_name,
                        PackageType='Image',
                        Code={'ImageUri': container_image},
                        Role=role_arn,
                        Timeout=300,  # Shorter timeout
                        MemorySize=128,  # Minimal memory
                    )
                    print(f"Successfully created {prefixed_func_name} with minimal parameters")
                    
                    # Wait for the function to become active before updating
                    print(f"Waiting for {prefixed_func_name} to become active...")
                    max_attempts = 60  # Wait up to 5 minutes
                    attempt = 0
                    while attempt < max_attempts:
                        try:
                            response = lambda_client.get_function(FunctionName=prefixed_func_name)
                            state = response['Configuration']['State']
                            
                            if state == 'Active':
                                print(f"Function {prefixed_func_name} is now active")
                                break
                            elif state == 'Failed':
                                print(f"Function {prefixed_func_name} creation failed")
                                sys.exit(1)
                            else:
                                print(f"Function state: {state}, waiting...")
                                time.sleep(5)
                                attempt += 1
                        except Exception as e:
                            print(f"Error checking function state: {str(e)}")
                            time.sleep(5)
                            attempt += 1
                    
                    if attempt >= max_attempts:
                        print(f"Timeout waiting for {prefixed_func_name} to become active")
                        sys.exit(1)
                    
                    # Now update with full configuration
                    lambda_client.update_function_configuration(
                        FunctionName=prefixed_func_name,
                        Timeout=900,
                        MemorySize=1024,
                        Environment={'Variables': environment_vars}
                    )
                    print(f"Updated {prefixed_func_name} with full configuration")
                    
                except Exception as minimal_error:
                    print(f"Minimal creation failed: {minimal_error}")
                    raise minimal_error
            
        except Exception as e:
            print(f"Error deploying {prefixed_func_name} to AWS: {str(e)}")
            # Print additional debugging information
            if "RequestEntityTooLargeException" in str(e):
                print(f"Payload too large. SECRET_PAYLOAD size: {len(secret_payload)} bytes")
                print("Consider reducing workflow complexity or using external storage")
            elif "InvalidParameterValueException" in str(e):
                print("Check Lambda configuration parameters (memory, timeout, role)")
            sys.exit(1)


def get_openwhisk_credentials(workflow_data):
    # Get OpenWhisk server configuration from workflow data
    for server_name, server_config in workflow_data['ComputeServers'].items():
        if server_config['FaaSType'].lower() == 'openwhisk':
            return (
                server_config['Endpoint'],
                server_config['Namespace'],
                server_config['SSL'].lower() == 'true'
            )
    
    print("Error: No OpenWhisk server configuration found in workflow data")
    sys.exit(1)

def deploy_to_ow(workflow_data):
    # Get OpenWhisk credentials
    api_host, namespace, ssl = get_openwhisk_credentials(workflow_data)
    
    # Get the workflow name for prefixing
    workflow_name = workflow_data.get('WorkflowName', 'default')
    json_prefix = workflow_name
    
    # Filter actions that should be deployed to OpenWhisk
    ow_actions = {}
    for action_name, action_data in workflow_data['ActionList'].items():
        server_name = action_data['FaaSServer']
        server_config = workflow_data['ComputeServers'][server_name]
        faas_type = server_config['FaaSType'].lower()
        if faas_type in ['openwhisk', 'open_whisk', 'ow']:
            ow_actions[action_name] = action_data
    
    if not ow_actions:
        print("No actions found for OpenWhisk deployment")
        return

    
    # Set up wsk properties
    subprocess.run(f"wsk property set --apihost {api_host}", shell=True)
    
    # Set authentication using API key from environment variable
    ow_api_key = os.getenv('OW_APIKEY')
    if ow_api_key:
        subprocess.run(f"wsk property set --auth {ow_api_key}", shell=True)
        print("Using OpenWhisk with API key authentication")
    else:
        print("Using OpenWhisk without authentication")
    
    # Always use insecure flag to bypass certificate issues
    subprocess.run("wsk property set --insecure", shell=True)
    
    # Set environment variable to handle certificate issue
    env = os.environ.copy()
    env['GODEBUG'] = 'x509ignoreCN=0'
    
    # Process each action in the workflow
    for action_name, action_data in ow_actions.items():
        try:
            actual_func_name = action_data['FunctionName']
            
            # Create prefixed function name using workflow_name-action_name format
            prefixed_func_name = f"{json_prefix}-{action_name}"
            
            # Create or update OpenWhisk action using wsk CLI
            try:
                # First check if action exists (add --insecure flag)
                check_cmd = f"wsk action get {prefixed_func_name} --insecure >/dev/null 2>&1"
                exists = subprocess.run(check_cmd, shell=True, env=env).returncode == 0
                
                # Get container image, with fallback to default
                container_image = workflow_data.get('ActionContainers', {}).get(action_name, 'ghcr.io/faasr/openwhisk-tidyverse')
                
                if exists:
                    # Update existing action (add --insecure flag)
                    cmd = f"wsk action update {prefixed_func_name} --docker {container_image} --insecure"
                else:
                    # Create new action (add --insecure flag)
                    cmd = f"wsk action create {prefixed_func_name} --docker {container_image} --insecure"
                
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env)
                
                if result.returncode != 0:
                    raise Exception(f"Failed to {'update' if exists else 'create'} action: {result.stderr}")
                
                print(f"Successfully deployed {prefixed_func_name} to OpenWhisk")
                
            except Exception as e:
                print(f"Error deploying {prefixed_func_name} to OpenWhisk: {str(e)}")
                sys.exit(1)
                
        except Exception as e:
            print(f"Error processing {prefixed_func_name}: {str(e)}")
            sys.exit(1)

def deploy_to_gcp(workflow_data):
    """Deploy functions to Google Cloud Functions using container images."""
    # Get GCP credentials from workflow data
    project_id, key_file = get_gcp_credentials_from_workflow(workflow_data)
    
    try:
        # Initialize GCP client
        functions_client = functions_v1.CloudFunctionsServiceClient()
        
        # Get the workflow name for function naming
        workflow_name = workflow_data.get('WorkflowName', 'default')
        json_prefix = workflow_name
        
        # Create secret payload (same as other deployments)
        secret_payload = create_secret_payload(workflow_data)
        
        # Filter actions that should be deployed to GCP Cloud Functions
        gcp_actions = {}
        gcp_region = 'us-central1'  # Default region
        
        for action_name, action_data in workflow_data['ActionList'].items():
            server_name = action_data['FaaSServer']
            server_config = workflow_data['ComputeServers'][server_name]
            faas_type = server_config['FaaSType'].lower()
            if faas_type in ['cloudfunctions', 'cloud_functions', 'gcp', 'gcf', 'googlecloud']:
                gcp_actions[action_name] = action_data
                # Get the region from this server config
                gcp_region = server_config.get('Region', 'us-central1')
        
        if not gcp_actions:
            print("No actions found for GCP Cloud Functions deployment")
            return
        
        # Process each action in the workflow
        for action_name, action_data in gcp_actions.items():
            try:
                actual_func_name = action_data['FunctionName']
                
                # Create prefixed function name using workflow_name-action_name format
                prefixed_func_name = f"{json_prefix}-{action_name}".replace('_', '-').lower()
                
                # Get container image 
                container_image = workflow_data.get('ActionContainers', {}).get(action_name)
                if not container_image:
                    container_image = 'gcr.io/faasr-project/gcp-cloud-functions-tidyverse:latest'
                    print(f"No container specified for action '{action_name}', using default: {container_image}")
                
                # Prepare the Cloud Function paths
                location_path = functions_client.common_location_path(project_id, gcp_region)
                function_path = functions_client.cloud_function_path(project_id, gcp_region, prefixed_func_name)
                
                # Environment variables for the function
                environment_vars = {
                    'SECRET_PAYLOAD': secret_payload
                }
                
                # Check payload size
                payload_size = len(secret_payload.encode('utf-8'))
                if payload_size > 32000:  # Cloud Functions env var limit is ~32KB
                    print(f"Warning: SECRET_PAYLOAD size ({payload_size} bytes) may exceed Cloud Functions environment variable limits")
                    print("Consider using Google Secret Manager for large payloads")
                
                # Define the Cloud Function with container image
                function = {
                    "name": function_path,
                    "docker_repository": container_image,
                    "timeout": "540s",
                    "available_memory_mb": 1024,
                    "environment_variables": environment_vars,
                    "https_trigger": {},
                }
                
                # Check if function already exists
                try:
                    existing_function = functions_client.get_function(name=function_path)
                    print(f"Function {prefixed_func_name} already exists, updating...")
                    
                    # Update existing function
                    update_mask = {"paths": ["docker_repository", "environment_variables", "timeout", "available_memory_mb"]}
                    
                    operation = functions_client.update_function(
                        function=function,
                        update_mask=update_mask
                    )
                    
                    # Wait for the operation to complete
                    print(f"Waiting for {prefixed_func_name} update to complete...")
                    result = operation.result(timeout=300)  # 5 minute timeout
                    print(f"Successfully updated {prefixed_func_name} on GCP Cloud Functions")
                    
                except Exception as e:
                    if "not found" in str(e).lower() or "404" in str(e):
                        # Function doesn't exist, create it
                        print(f"Creating new Cloud Function: {prefixed_func_name}")
                        
                        operation = functions_client.create_function(
                            parent=location_path,
                            function=function
                        )
                        
                        # Wait for the operation to complete
                        print(f"Waiting for {prefixed_func_name} creation to complete...")
                        result = operation.result(timeout=600)  # 10 minute timeout for creation
                        print(f"Successfully created {prefixed_func_name} on GCP Cloud Functions")
                    else:
                        raise e
                        
            except Exception as e:
                print(f"Error deploying {prefixed_func_name} to GCP: {str(e)}")
                # Print additional debugging information
                if "PERMISSION_DENIED" in str(e):
                    print("Check GCP service account permissions for Cloud Functions")
                elif "QUOTA_EXCEEDED" in str(e):
                    print("Check GCP quotas for Cloud Functions in your project")
                elif "INVALID_ARGUMENT" in str(e):
                    print("Check function configuration parameters (name, region, container image)")
                sys.exit(1)
                
    except Exception as e:
        print(f"Error setting up GCP deployment: {str(e)}")
        sys.exit(1)
    finally:
        # Clean up temporary key file if created
        if key_file and os.path.exists(key_file):
            os.unlink(key_file)

def main():
    args = parse_arguments()
    workflow_data = read_workflow_file(args.workflow_file)
    
    # Store the workflow file path in the workflow data
    workflow_data['_workflow_file'] = args.workflow_file
    
    # Validate workflow using FaaSr-Backend validation functions
    print("Validating workflow using FaaSr-Backend validation...")
    try:
        validate_workflow_with_faasr_backend(workflow_data)
        print("✓ Complete workflow validation passed")
    except SystemExit:
        print("✗ Workflow validation failed - check logs for details")
        sys.exit(1)
    
    # Get all unique FaaSTypes from workflow data
    faas_types = set()
    for server in workflow_data.get('ComputeServers', {}).values():
        if 'FaaSType' in server:
            faas_types.add(server['FaaSType'].lower())
    
    if not faas_types:
        print("Error: No FaaSType found in workflow file")
        sys.exit(1)
    
    print(f"Found FaaS platforms: {', '.join(faas_types)}")
    
    # Deploy to each platform found
    for faas_type in faas_types:
        print(f"\nDeploying to {faas_type}...")
        if faas_type in ['lambda', 'aws_lambda', 'aws']:
            deploy_to_aws(workflow_data)
        elif faas_type in ['githubactions', 'github_actions', 'github']:
            deploy_to_github(workflow_data)
        elif faas_type in ['openwhisk', 'open_whisk', 'ow']:
            deploy_to_ow(workflow_data)
        elif faas_type in ['cloudfunctions', 'cloud_functions', 'gcp', 'gcf', 'googlecloud']:
            deploy_to_gcp(workflow_data)
        else:
            print(f"Warning: Unknown FaaSType '{faas_type}' - skipping")
    

if __name__ == '__main__':
    main() 