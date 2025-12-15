#!/usr/bin/env python3

import argparse
import json
import logging
import os
import sys

import boto3
from botocore.exceptions import ClientError
from google.cloud import secretmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Sync GitHub secrets to AWS Secrets Manager and/or GCP Secret Manager"
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


def sync_all_secrets_to_aws(secrets_manager_client, secrets):
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
    
    logger.info(f"\nAWS Sync complete: {success_count} succeeded, {failure_count} failed")
    
    return failure_count == 0


def get_gcp_config(workflow_data, secrets):
    """Extract GCP configuration from workflow file and secrets"""
    # Get GCP secret key (PEM format service account key)
    gcp_secret_key = secrets.get("GCP_SECRETKEY") or secrets.get("GCP_SecretKey")
    
    if not gcp_secret_key:
        logger.error("GCP_SECRETKEY not found in secrets")
        sys.exit(1)
    
    # Find GCP configuration in ComputeServers
    compute_servers = workflow_data.get("ComputeServers", {})
    gcp_config = None
    
    for server_config in compute_servers.values():
        faas_type = server_config.get("FaaSType", "")
        if faas_type.lower() == "googlecloud":
            gcp_config = server_config
            break
    
    if not gcp_config:
        logger.error("No GoogleCloud configuration found in workflow ComputeServers")
        sys.exit(1)
    
    # Extract required fields
    project_id = gcp_config.get("Namespace")
    client_email = gcp_config.get("ClientEmail")
    
    if not project_id:
        logger.error("Namespace (project ID) not found in GCP configuration")
        sys.exit(1)
    
    if not client_email:
        logger.error("ClientEmail not found in GCP configuration")
        sys.exit(1)
    
    logger.info(f"GCP configuration extracted: project={project_id}, email={client_email}")
    
    return gcp_secret_key, project_id, client_email


def sync_secret_to_gcp(client, project_id, secret_name, secret_value):
    """
    Sync a single secret to GCP Secret Manager.
    Creates the secret if it doesn't exist, adds a new version if it does.
    """
    parent = f"projects/{project_id}"
    secret_path = f"{parent}/secrets/{secret_name}"
    
    try:
        # Try to get the secret to see if it exists
        client.get_secret(request={"name": secret_path})
        
        # Secret exists, add a new version
        try:
            parent_version = secret_path
            payload = secret_value.encode("UTF-8")
            
            client.add_secret_version(
                request={
                    "parent": parent_version,
                    "payload": {"data": payload}
                }
            )
            logger.info(f"Updated secret: {secret_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to update secret {secret_name}: {e}")
            return False
            
    except Exception as e:
        if "NOT_FOUND" in str(e) or "404" in str(e):
            # Secret doesn't exist, create it
            try:
                secret = client.create_secret(
                    request={
                        "parent": parent,
                        "secret_id": secret_name,
                        "secret": {
                            "replication": {"automatic": {}}
                        }
                    }
                )
                
                # Add the first version
                payload = secret_value.encode("UTF-8")
                client.add_secret_version(
                    request={
                        "parent": secret.name,
                        "payload": {"data": payload}
                    }
                )
                logger.info(f"Created secret: {secret_name}")
                return True
            except Exception as create_error:
                logger.error(f"Failed to create secret {secret_name}: {create_error}")
                return False
        else:
            logger.error(f"Failed to check secret {secret_name}: {e}")
            return False


def sync_all_secrets_to_gcp(client, project_id, secrets):
    """Sync all GitHub secrets to GCP Secret Manager"""
    success_count = 0
    failure_count = 0
    
    logger.info(f"\nStarting sync of {len(secrets)} secrets to GCP Secret Manager...")
    
    for secret_name, secret_value in secrets.items():
        # Convert secret value to string if it's not already
        secret_value_str = str(secret_value) if secret_value is not None else ""
        
        if sync_secret_to_gcp(client, project_id, secret_name, secret_value_str):
            success_count += 1
        else:
            failure_count += 1
    
    logger.info(f"\nGCP Sync complete: {success_count} succeeded, {failure_count} failed")
    
    return failure_count == 0


def main():
    args = parse_arguments()
    
    # Read workflow file
    workflow_data = read_workflow_file(args.workflow_file)
    logger.info(f"Successfully loaded workflow file: {args.workflow_file}")
    
    # Read GitHub secrets
    secrets = read_github_secrets()
    
    # Get sync targets from environment variables
    sync_to_aws = os.getenv("SYNC_TO_AWS", "false").lower() == "true"
    sync_to_gcp = os.getenv("SYNC_TO_GCP", "false").lower() == "true"
    
    if not sync_to_aws and not sync_to_gcp:
        logger.error("No sync target specified. Set SYNC_TO_AWS or SYNC_TO_GCP to true")
        sys.exit(1)
    
    logger.info(f"Sync targets - AWS: {sync_to_aws}, GCP: {sync_to_gcp}")
    
    all_success = True
    
    # Sync to AWS if enabled
    if sync_to_aws:
        logger.info("\n" + "="*60)
        logger.info("SYNCING TO AWS SECRETS MANAGER")
        logger.info("="*60)
        
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
            all_success = False
        else:
            # Sync all secrets to AWS
            if not sync_all_secrets_to_aws(secrets_manager_client, secrets):
                all_success = False
    
    # Sync to GCP if enabled
    if sync_to_gcp:
        logger.info("\n" + "="*60)
        logger.info("SYNCING TO GCP SECRET MANAGER")
        logger.info("="*60)
        
        # Get GCP configuration
        try:
            gcp_secret_key, project_id, client_email = get_gcp_config(workflow_data, secrets)
            logger.info(f"Retrieved GCP secret key (length: {len(gcp_secret_key)})")
        except Exception as e:
            import traceback
            logger.error(f"Failed to get GCP config: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            all_success = False
            gcp_secret_key = None
        
        # Use FaaSr_py helper to handle PEM format and get access token
        if gcp_secret_key:
            try:
                logger.info("Importing FaaSr_py helper...")
                from FaaSr_py.helpers.gcp_auth import refresh_gcp_access_token
                logger.info("Successfully imported FaaSr_py helper")
                
                # Find the GCP server name from workflow
                gcp_server_name = None
                for server_name, server_config in workflow_data.get("ComputeServers", {}).items():
                    if server_config.get("FaaSType", "").lower() == "googlecloud":
                        gcp_server_name = server_name
                        break
                
                logger.info(f"Found GCP server: {gcp_server_name}")
                
                if not gcp_server_name:
                    logger.error("No GoogleCloud server found in ComputeServers")
                    all_success = False
                else:
                    # Build temp payload for FaaSr authentication
                    logger.info("Building temp payload for authentication...")
                    gcp_server_config = workflow_data["ComputeServers"][gcp_server_name].copy()
                    gcp_server_config["SecretKey"] = gcp_secret_key
                    
                    # Ensure Region is set (default to us-central1 if not specified)
                    if "Region" not in gcp_server_config:
                        gcp_server_config["Region"] = "us-central1"
                        logger.warning("GCP Region not specified in workflow, defaulting to us-central1")
                    
                    temp_payload = {"ComputeServers": {gcp_server_name: gcp_server_config}}
                    
                    # Get access token using FaaSr helper (handles PEM format)
                    logger.info("Calling refresh_gcp_access_token...")
                    access_token = refresh_gcp_access_token(temp_payload, gcp_server_name)
                    logger.info("Successfully authenticated with GCP using access token")
                    
                    # Create credentials from access token
                    logger.info("Creating credentials from access token...")
                    from google.auth.transport import requests as google_requests
                    from google.oauth2 import credentials as oauth2_credentials
                    
                    credentials = oauth2_credentials.Credentials(token=access_token)
                    
                    # Initialize GCP Secret Manager client with access token
                    logger.info("Initializing GCP Secret Manager client...")
                    gcp_client = secretmanager.SecretManagerServiceClient(credentials=credentials)
                    logger.info("Successfully initialized GCP Secret Manager client")
                    
                    # Sync all secrets to GCP
                    logger.info("Starting secret sync to GCP...")
                    if not sync_all_secrets_to_gcp(gcp_client, project_id, secrets):
                        all_success = False
                        
            except Exception as e:
                import traceback
                logger.error(f"Failed during GCP sync: {e}")
                logger.error(f"Traceback: {traceback.format_exc()}")
                all_success = False
    
    # Final status
    logger.info("\n" + "="*60)
    if all_success:
        logger.info("✓ Secret sync completed successfully!")
    else:
        logger.error("✗ Secret sync completed with errors")
        sys.exit(1)


if __name__ == "__main__":
    main()

