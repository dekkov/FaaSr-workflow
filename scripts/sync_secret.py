#!/usr/bin/env python3

import argparse
import json
import logging
import os
import sys

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Sync GitHub secrets to AWS Secrets Manager"
    )
    parser.add_argument(
        "--workflow-file", required=True, help="Path to the workflow JSON file"
    )
    return parser.parse_args()


def read_workflow_file(file_path):
    """Read and parse the workflow JSON file"""
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Error: Workflow file {file_path} not found")
        sys.exit(1)
    except json.JSONDecodeError:
        logger.error(f"Error: Invalid JSON in workflow file {file_path}")
        sys.exit(1)


def read_github_secrets():
    """Read GitHub secrets from ALL_SECRETS_JSON environment variable"""
    secrets_json = os.getenv("ALL_SECRETS_JSON")
    
    if not secrets_json:
        logger.error("ALL_SECRETS_JSON environment variable not set")
        sys.exit(1)
    
    try:
        secrets = json.loads(secrets_json)
        logger.info(f"Successfully loaded {len(secrets)} secrets from environment")
        return secrets
    except json.JSONDecodeError:
        logger.error("Error: Invalid JSON in ALL_SECRETS_JSON environment variable")
        sys.exit(1)


def get_aws_credentials(secrets):
    """Extract AWS credentials from secrets"""
    # Try different possible key names for AWS credentials
    aws_access_key = (
        secrets.get("AWS_ACCESSKEY", "")
    )
    
    aws_secret_key = (
        secrets.get("AWS_SECRETKEY", "")
    )
    
    if not aws_access_key or not aws_secret_key:
        logger.error(
            "AWS credentials not found in secrets. "
            "Expected AWS_ACCESSKEY and AWS_SECRETKEY (or AWS_AccessKey and AWS_SecretKey)"
        )
        sys.exit(1)
    
    logger.info("AWS credentials successfully extracted from secrets")
    return aws_access_key, aws_secret_key


def get_aws_region(workflow_data):
    """Extract AWS region from workflow file, default to us-east-1"""
    # Try to get region from ComputeServers or DataStores
    region = None
    
    # Check ComputeServers for AWS/Lambda configuration
    compute_servers = workflow_data.get("ComputeServers", {})
    for server_config in compute_servers.values():
        faas_type = server_config.get("FaaSType", "")
        if faas_type.lower() in ["lambda", "aws_lambda", "aws"]:
            region = server_config.get("Region")
            if region:
                break
    
    # Check DataStores for S3 region
    if not region:
        data_stores = workflow_data.get("DataStores", {})
        for store_config in data_stores.values():
            region = store_config.get("Region")
            if region:
                break
    
    # Default to us-east-1
    if not region:
        region = "us-east-1"
        logger.warning(f"AWS region not specified in workflow file, defaulting to {region}")
    else:
        logger.info(f"Using AWS region: {region}")
    
    return region


def sync_secret_to_aws(secrets_manager_client, secret_name, secret_value):
    """
    Sync a single secret to AWS Secrets Manager.
    Creates the secret if it doesn't exist, updates it if it does.
    """
    try:
        # Try to describe the secret to see if it exists
        secrets_manager_client.describe_secret(SecretId=secret_name)
        
        # Secret exists, update it
        try:
            secrets_manager_client.update_secret(
                SecretId=secret_name,
                SecretString=secret_value
            )
            logger.info(f"Updated secret: {secret_name}")
            return True
        except ClientError as e:
            logger.error(f"Failed to update secret {secret_name}: {e}")
            return False
            
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            # Secret doesn't exist, create it
            try:
                secrets_manager_client.create_secret(
                    Name=secret_name,
                    SecretString=secret_value
                )
                logger.info(f"Created secret: {secret_name}")
                return True
            except ClientError as create_error:
                logger.error(f"Failed to create secret {secret_name}: {create_error}")
                return False
        else:
            logger.error(f"Failed to check secret {secret_name}: {e}")
            return False


def sync_all_secrets(secrets_manager_client, secrets):
    """Sync all GitHub secrets to AWS Secrets Manager"""
    success_count = 0
    failure_count = 0
    
    logger.info(f"\nStarting sync of {len(secrets)} secrets to AWS Secrets Manager...")
    
    for secret_name, secret_value in secrets.items():
        # Convert secret value to string if it's not already
        secret_value_str = str(secret_value) if secret_value is not None else ""
        
        if sync_secret_to_aws(secrets_manager_client, secret_name, secret_value_str):
            success_count += 1
        else:
            failure_count += 1
    
    logger.info(f"\nSync complete: {success_count} succeeded, {failure_count} failed")
    
    if failure_count > 0:
        sys.exit(1)


def main():
    args = parse_arguments()
    
    # Read workflow file
    workflow_data = read_workflow_file(args.workflow_file)
    logger.info(f"Successfully loaded workflow file: {args.workflow_file}")
    
    # Read GitHub secrets
    secrets = read_github_secrets()
    
    # Get AWS credentials from secrets
    aws_access_key, aws_secret_key = get_aws_credentials(secrets)
    
    # Get AWS region from workflow file
    aws_region = get_aws_region(workflow_data)
    
    # Initialize AWS Secrets Manager client
    try:
        secrets_manager_client = boto3.client(
            'secretsmanager',
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            region_name=aws_region
        )
        logger.info("Successfully initialized AWS Secrets Manager client")
    except Exception as e:
        logger.error(f"Failed to initialize AWS Secrets Manager client: {e}")
        sys.exit(1)
    
    # Sync all secrets
    sync_all_secrets(secrets_manager_client, secrets)
    
    logger.info("Secret sync completed successfully!")


if __name__ == "__main__":
    main()

