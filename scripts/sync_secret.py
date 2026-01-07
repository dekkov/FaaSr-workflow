#!/usr/bin/env python3

import argparse
import base64
import json
import logging
import os
import sys

import boto3
import requests
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
        description="Sync GitHub secrets to AWS Secrets Manager and/or GCP Secret Manager"
    )
    parser.add_argument("--workflow-file", required=True, help="Path to the workflow JSON file")
    return parser.parse_args()


def read_workflow_file(file_path):
    """Read and parse the workflow JSON file"""
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Workflow file {file_path} not found")
        sys.exit(1)
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON in workflow file {file_path}")
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
        logger.error("Invalid JSON in ALL_SECRETS_JSON environment variable")
        sys.exit(1)


def get_aws_config(workflow_data, secrets):
    """Extract AWS credentials and region from secrets and workflow"""
    aws_access_key = secrets.get("AWS_ACCESSKEY", "")
    aws_secret_key = secrets.get("AWS_SECRETKEY", "")
    
    if not aws_access_key or not aws_secret_key:
        logger.error("AWS_ACCESSKEY and AWS_SECRETKEY not found in secrets")
        sys.exit(1)
    
    # Get region from ComputeServers or DataStores
    region = None
    for server_config in workflow_data.get("ComputeServers", {}).values():
        if server_config.get("FaaSType", "").lower() in ["lambda", "aws_lambda", "aws"]:
            region = server_config.get("Region")
            if region:
                break
    
    if not region:
        for store_config in workflow_data.get("DataStores", {}).values():
            region = store_config.get("Region")
            if region:
                break
    
    region = region or "us-east-1"
    logger.info(f"AWS credentials extracted, using region: {region}")
    return aws_access_key, aws_secret_key, region


def sync_secret_to_aws(client, secret_name, secret_value):
    """Sync a single secret to AWS Secrets Manager"""
    try:
        client.describe_secret(SecretId=secret_name)
        client.update_secret(SecretId=secret_name, SecretString=secret_value)
        logger.info(f"Updated secret: {secret_name}")
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceNotFoundException':
            try:
                client.create_secret(Name=secret_name, SecretString=secret_value)
                logger.info(f"Created secret: {secret_name}")
                return True
            except ClientError as create_error:
                logger.error(f"Failed to create secret {secret_name}: {create_error}")
                return False
        else:
            logger.error(f"Failed to check/update secret {secret_name}: {e}")
            return False


def sync_all_secrets_to_aws(client, secrets):
    """Sync all GitHub secrets to AWS Secrets Manager"""
    logger.info(f"Starting sync of {len(secrets)} secrets to AWS...")
    success = sum(1 for name, value in secrets.items() 
                  if sync_secret_to_aws(client, name, str(value) if value else ""))
    logger.info(f"AWS Sync complete: {success}/{len(secrets)} succeeded")
    return success == len(secrets)


def get_gcp_config(workflow_data, secrets):
    """Extract GCP configuration from workflow file and secrets"""
    gcp_secret_key = secrets.get("GCP_SECRETKEY") or secrets.get("GCP_SecretKey")
    
    if not gcp_secret_key:
        logger.error("GCP_SECRETKEY not found in secrets")
        sys.exit(1)
    
    # Normalize PEM key: replace escaped newlines with actual newlines
    if '\\n' in gcp_secret_key:
        gcp_secret_key = gcp_secret_key.replace('\\n', '\n')
    
    # Find GCP configuration in ComputeServers
    gcp_config = None
    for server_config in workflow_data.get("ComputeServers", {}).values():
        if server_config.get("FaaSType", "").lower() == "googlecloud":
            gcp_config = server_config
            break
    
    if not gcp_config:
        logger.error("No GoogleCloud configuration found in workflow ComputeServers")
        sys.exit(1)
    
    project_id = gcp_config.get("Namespace")
    client_email = gcp_config.get("ClientEmail")
    
    if not project_id or not client_email:
        logger.error("Namespace (project ID) or ClientEmail not found in GCP configuration")
        sys.exit(1)
    
    logger.info(f"GCP configuration extracted: project={project_id}")
    return gcp_secret_key, project_id, client_email


def sync_secret_to_gcp(headers, project_id, secret_name, secret_value):
    """Sync a single secret to GCP Secret Manager using REST API"""
    base_url = f"https://secretmanager.googleapis.com/v1/projects/{project_id}/secrets"
    secret_url = f"{base_url}/{secret_name}"
    
    try:
        response = requests.get(secret_url, headers=headers)
        encoded_payload = base64.b64encode(secret_value.encode("UTF-8")).decode("UTF-8")
        version_body = {"payload": {"data": encoded_payload}}
        
        if response.status_code == 200:
            # Secret exists, add new version
            version_response = requests.post(f"{secret_url}:addVersion", json=version_body, headers=headers)
            if version_response.status_code in [200, 201]:
                logger.info(f"Updated secret: {secret_name}")
                return True
            logger.error(f"Failed to update secret {secret_name}: {version_response.text}")
            return False
            
        elif response.status_code == 404:
            # Secret doesn't exist, create it
            create_body = {"replication": {"automatic": {}}}
            create_response = requests.post(f"{base_url}?secretId={secret_name}", 
                                          json=create_body, headers=headers)
            
            if create_response.status_code in [200, 201]:
                logger.info(f"Created secret: {secret_name}")
                version_response = requests.post(f"{secret_url}:addVersion", 
                                               json=version_body, headers=headers)
                if version_response.status_code in [200, 201]:
                    return True
                logger.error(f"Failed to add version to secret {secret_name}: {version_response.text}")
                return False
            logger.error(f"Failed to create secret {secret_name}: {create_response.text}")
            return False
        else:
            logger.error(f"Failed to check secret {secret_name}: {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Exception while syncing secret {secret_name}: {e}")
        return False


def sync_all_secrets_to_gcp(headers, project_id, secrets):
    """Sync all GitHub secrets to GCP Secret Manager"""
    logger.info(f"Starting sync of {len(secrets)} secrets to GCP...")
    success = sum(1 for name, value in secrets.items() 
                  if sync_secret_to_gcp(headers, project_id, name, str(value) if value else ""))
    logger.info(f"GCP Sync complete: {success}/{len(secrets)} succeeded")
    return success == len(secrets)


def main():
    args = parse_arguments()
    workflow_data = read_workflow_file(args.workflow_file)
    logger.info(f"Successfully loaded workflow file: {args.workflow_file}")
    
    secrets = read_github_secrets()
    
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
        
        try:
            aws_access_key, aws_secret_key, aws_region = get_aws_config(workflow_data, secrets)
            client = boto3.client('secretsmanager',
                                aws_access_key_id=aws_access_key,
                                aws_secret_access_key=aws_secret_key,
                                region_name=aws_region)
            
            if not sync_all_secrets_to_aws(client, secrets):
                all_success = False
        except Exception as e:
            logger.error(f"AWS sync failed: {e}")
            all_success = False
    
    # Sync to GCP if enabled
    if sync_to_gcp:
        logger.info("\n" + "="*60)
        logger.info("SYNCING TO GCP SECRET MANAGER")
        logger.info("="*60)
        
        try:
            gcp_secret_key, project_id, client_email = get_gcp_config(workflow_data, secrets)
            
            # Find GCP server name and build payload for authentication
            gcp_server_name = None
            for server_name, server_config in workflow_data.get("ComputeServers", {}).items():
                if server_config.get("FaaSType", "").lower() == "googlecloud":
                    gcp_server_name = server_name
                    break
            
            if not gcp_server_name:
                logger.error("No GoogleCloud server found in ComputeServers")
                all_success = False
            else:
                from FaaSr_py.helpers.gcp_auth import refresh_gcp_access_token
                
                gcp_server_config = workflow_data["ComputeServers"][gcp_server_name].copy()
                gcp_server_config["SecretKey"] = gcp_secret_key
                gcp_server_config.setdefault("Region", "us-central1")
                
                temp_payload = {"ComputeServers": {gcp_server_name: gcp_server_config}}
                access_token = refresh_gcp_access_token(temp_payload, gcp_server_name)
                
                headers = {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {access_token}",
                }
                
                if not sync_all_secrets_to_gcp(headers, project_id, secrets):
                    all_success = False
                    
        except Exception as e:
            import traceback
            logger.error(f"GCP sync failed: {e}")
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
