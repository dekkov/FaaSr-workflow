#!/usr/bin/env python3

import argparse
import json
import os
import sys
from pathlib import Path

# Add the FaaSr-Backend to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'FaaSr-Backend'))

from FaaSr_py.engine.scheduler import Scheduler
from FaaSr_py.engine.faasr_payload import FaaSrPayload


class WorkflowMigrationAdapter:
    """
    Adapter class that bridges the gap between the current invoke_workflow.py 
    approach and the Scheduler class from FaaSr-Backend.
    """
    
    def __init__(self, workflow_file_path):
        """
        Initialize the migration adapter.
        
        Args:
            workflow_file_path (str): Path to the workflow JSON file
        """
        self.workflow_file_path = workflow_file_path
        self.workflow_data = self._read_workflow_file()
        self.faasr_payload = None
        
    def _read_workflow_file(self):
        """Read and parse the workflow JSON file."""
        try:
            with open(self.workflow_file_path, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"Error: Workflow file {self.workflow_file_path} not found")
            sys.exit(1)
        except json.JSONDecodeError:
            print(f"Error: Invalid JSON in workflow file {self.workflow_file_path}")
            sys.exit(1)
    
    def _get_credentials(self):
        """Get credentials from environment variables."""
        return {
            "My_GitHub_Account_TOKEN": os.getenv('GH_PAT'),
            "My_Minio_Bucket_ACCESS_KEY": os.getenv('MINIO_ACCESSKEY'),
            "My_Minio_Bucket_SECRET_KEY": os.getenv('MINIO_SECRETKEY'),
            "My_OW_Account_API_KEY": os.getenv('OW_APIKEY', ''),
            "My_Lambda_Account_ACCESS_KEY": os.getenv('AWS_ACCESSKEY', ''),
            "My_Lambda_Account_SECRET_KEY": os.getenv('AWS_SECRETKEY', ''),
            "My_GCP_Account_PROJECT_ID": os.getenv('GCP_PROJECT_ID', ''),
            "My_GCP_Account_SERVICE_ACCOUNT_KEY": os.getenv('GCP_SECRETKEY', ''),
            "My_SLURM_Account_TOKEN": os.getenv('SLURM_TOKEN', ''),
        }
    
    def _replace_credential_placeholders(self, workflow_data):
        """
        Replace credential placeholders in the workflow data with actual values.
        Uses flexible pattern matching like register_workflow.py
        
        Args:
            workflow_data (dict): The workflow configuration
            
        Returns:
            dict: Updated workflow data with credentials
        """
        credentials = self._get_credentials()
        workflow_copy = workflow_data.copy()
        
        # Replace placeholder values in ComputeServers with actual credentials
        if 'ComputeServers' in workflow_copy:
            for server_key, server_config in workflow_copy['ComputeServers'].items():
                faas_type = server_config.get('FaaSType', '')
                
                # Replace placeholder values with actual credentials using pattern matching
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
                    # Replace GCP credentials placeholders
                    # Handle Namespace field (project ID)
                    if 'Namespace' in server_config and server_config['Namespace'] == f"{server_key}_PROJECT_ID":
                        if credentials['My_GCP_Account_PROJECT_ID']:
                            server_config['Namespace'] = credentials['My_GCP_Account_PROJECT_ID']
                    # Handle ProjectID field (for backward compatibility)
                    if 'ProjectID' in server_config and server_config['ProjectID'] == f"{server_key}_PROJECT_ID":
                        if credentials['My_GCP_Account_PROJECT_ID']:
                            server_config['ProjectID'] = credentials['My_GCP_Account_PROJECT_ID']
                    # Handle ServiceAccountKey field
                    if 'ServiceAccountKey' in server_config and server_config['ServiceAccountKey'] == f"{server_key}_SERVICE_ACCOUNT_KEY":
                        if credentials['My_GCP_Account_SERVICE_ACCOUNT_KEY']:
                            server_config['ServiceAccountKey'] = credentials['My_GCP_Account_SERVICE_ACCOUNT_KEY']
                    # Handle SecretKey field (as seen in gcp.json)
                    if 'SecretKey' in server_config and server_config['SecretKey'] in [f"{server_key}_SECRET_KEY", "GCP_SECRET_KEY"]:
                        if credentials['My_GCP_Account_SERVICE_ACCOUNT_KEY']:
                            server_config['SecretKey'] = credentials['My_GCP_Account_SERVICE_ACCOUNT_KEY']

        # Replace placeholder values in DataStores with actual credentials
        if 'DataStores' in workflow_copy:
            for store_key, store_config in workflow_copy['DataStores'].items():
                # Replace placeholder values with actual credentials
                if 'AccessKey' in store_config and store_config['AccessKey'] == f"{store_key}_ACCESS_KEY":
                    if store_key == 'My_Minio_Bucket' and credentials['My_Minio_Bucket_ACCESS_KEY']:
                        store_config['AccessKey'] = credentials['My_Minio_Bucket_ACCESS_KEY']
                if 'SecretKey' in store_config and store_config['SecretKey'] == f"{store_key}_SECRET_KEY":
                    if store_key == 'My_Minio_Bucket' and credentials['My_Minio_Bucket_SECRET_KEY']:
                        store_config['SecretKey'] = credentials['My_Minio_Bucket_SECRET_KEY']
        
        return workflow_copy
    
    def _create_github_hosted_workflow(self):
        """
        Create a temporary GitHub-hosted workflow file and return its URL.
        
        Returns:
            str: The GitHub raw URL for the workflow file
        """
        # For migration purposes, we'll create a local mock URL
        # In a real deployment, this would be hosted on GitHub
        workflow_filename = os.path.basename(self.workflow_file_path)
        
        # Get GitHub info from the workflow data
        function_invoke = self.workflow_data.get('FunctionInvoke')
        if not function_invoke:
            print("Error: No FunctionInvoke specified in workflow file")
            sys.exit(1)
            
        action_data = self.workflow_data['ActionList'][function_invoke]
        server_name = action_data['FaaSServer']
        server_config = self.workflow_data['ComputeServers'][server_name]
        
        username = server_config.get('UserName', 'default-user')
        reponame = server_config.get('ActionRepoName', 'default-repo')
        branch = server_config.get('Branch', 'main')
        
        # Create GitHub raw URL format
        github_url = f"{username}/{reponame}/{branch}/{workflow_filename}"
        return github_url
    
    def _create_faasr_payload_from_local_file(self):
        """
        Create a FaaSrPayload instance from the local workflow file.
        
        Returns:
            FaaSrPayload: Configured payload instance
        """
        # Replace credential placeholders
        processed_workflow = self._replace_credential_placeholders(self.workflow_data)
        
        # Create a mock GitHub URL for the payload
        github_url = self._create_github_hosted_workflow()
        
        # Create overwritten fields to pass the processed workflow
        overwritten_fields = processed_workflow.copy()
        
        # Create a temporary local payload that mimics the GitHub structure
        # We'll monkey-patch the FaaSrPayload to work with our local data
        try:
            # Create a minimal FaaSrPayload instance
            # We'll override its initialization to use our local data
            payload = FaaSrPayloadAdapter(github_url, overwritten_fields, processed_workflow)
            return payload
        except Exception as e:
            print(f"Error creating FaaSrPayload: {e}")
            # Fallback: create a simple mock payload
            return FaaSrPayload(processed_workflow)
    
    def trigger_workflow(self):
        """
        Trigger the workflow using the Scheduler class.
        """
        # Get the function to invoke
        function_invoke = self.workflow_data.get('FunctionInvoke')
        if not function_invoke:
            print("Error: No FunctionInvoke specified in workflow file")
            sys.exit(1)
        
        if function_invoke not in self.workflow_data['ActionList']:
            print(f"Error: FunctionInvoke '{function_invoke}' not found in ActionList")
            sys.exit(1)
        
        # Get action and server configuration
        action_data = self.workflow_data['ActionList'][function_invoke]
        server_name = action_data['FaaSServer']
        server_config = self.workflow_data['ComputeServers'][server_name]
        faas_type = server_config['FaaSType'].lower()
        
        print(f"Migrating to Scheduler-based invocation for '{function_invoke}' on {faas_type}...")
        
        # Create FaaSrPayload instance
        self.faasr_payload = self._create_faasr_payload_from_local_file()
        
        # Create Scheduler instance
        try:
            scheduler = Scheduler(self.faasr_payload)
        except Exception as e:
            print(f"Error creating Scheduler: {e}")
            sys.exit(1)
        
        # Get workflow name for prefixing (if available)
        workflow_name = self.workflow_data.get('WorkflowName', '')
        
        # Use the Scheduler to trigger the function
        # This replaces all the individual trigger_* methods from invoke_workflow.py
        try:
            print(f"✓ Using Scheduler to trigger function: {function_invoke}")
            scheduler.trigger_func(workflow_name, function_invoke)
            print("✓ Workflow triggered successfully using Scheduler!")
        except Exception as e:
            print(f"✗ Error triggering workflow with Scheduler: {e}")
            sys.exit(1)


class FaaSrPayloadAdapter(FaaSrPayload):
    """
    Adapter that allows FaaSrPayload to work with local workflow files
    instead of requiring GitHub-hosted files.
    """
    
    def __init__(self, url, overwritten, local_workflow_data):
        """
        Initialize with local workflow data instead of fetching from GitHub.
        
        Args:
            url (str): Mock GitHub URL
            overwritten (dict): Overwritten fields
            local_workflow_data (dict): Local workflow configuration
        """
        self.url = url
        self._overwritten = overwritten or {}
        self._base_workflow = local_workflow_data
        
        # Set up log file name
        if self.get("FunctionRank"):
            self.log_file = f"{self['FunctionInvoke']}({self['FunctionRank']}).txt"
        else:
            self.log_file = f"{self['FunctionInvoke']}.txt"



def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Migration adapter: Trigger FaaSr workflow using Scheduler class'
    )
    parser.add_argument('--workflow-file', required=True,
                      help='Path to the workflow JSON file')
    parser.add_argument('--dry-run', action='store_true',
                      help='Show what would be done without actually triggering')
    return parser.parse_args()


def main():
    """Main entry point for the migration adapter."""
    print("=" * 60)
    print("FaaSr Workflow Migration Adapter")
    print("Migrating from invoke_workflow.py to Scheduler-based invocation")
    print("=" * 60)
    
    args = parse_arguments()
    
    # Verify workflow file exists
    if not os.path.exists(args.workflow_file):
        print(f"Error: Workflow file {args.workflow_file} not found")
        sys.exit(1)
    
    # Create migration adapter
    try:
        adapter = WorkflowMigrationAdapter(args.workflow_file)
    except Exception as e:
        print(f"Error initializing migration adapter: {e}")
        sys.exit(1)
    
    if args.dry_run:
        print("DRY RUN MODE - No actual invocation will occur")
        print(f"Would trigger function: {adapter.workflow_data.get('FunctionInvoke')}")
        print("Migration adapter initialized successfully!")
        return
    
    # Trigger the workflow using the new Scheduler approach
    try:
        adapter.trigger_workflow()
        print("\n" + "=" * 60)
        print("Migration completed successfully!")
        print("The workflow has been triggered using the Scheduler class.")
        print("=" * 60)
    except Exception as e:
        print(f"\nMigration failed: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
