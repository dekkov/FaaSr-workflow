import base64
import json
import time

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def refresh_gcp_access_token(faasr_payload, server_name):
    """
    Generate a new access token using JWT for GCP authentication.
    """
    server_config = faasr_payload["ComputeServers"][server_name]

    client_email = server_config["ClientEmail"]
    private_key = server_config["SecretKey"]
    token_uri = server_config["TokenUri"]
    
    # Handle escaped newlines in the private key
    # GitHub secrets often store \n as literal "\\n" string
    if "\\n" in private_key:
        private_key = private_key.replace("\\n", "\n")

    # Create JWT header and claims
    header = {"alg": "RS256", "typ": "JWT"}

    issued_at = int(time.time())
    expires_at = issued_at + 600  # Valid for 10 minutes

    claims = {
        "iss": client_email,
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "aud": token_uri,
        "exp": expires_at,
        "iat": issued_at,
    }

    # Base64 URL-safe encoding
    def base64url_encode(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

    # Encode header and claims
    jwt_header = base64url_encode(json.dumps(header).encode())
    jwt_claims = base64url_encode(json.dumps(claims).encode())

    # Create unsigned JWT
    jwt_unsigned = f"{jwt_header}.{jwt_claims}"

    # Sign the JWT
    private_key_obj = serialization.load_pem_private_key(
        private_key.encode(), password=None, backend=default_backend()
    )

    signature = private_key_obj.sign(
        jwt_unsigned.encode(), padding.PKCS1v15(), hashes.SHA256()
    )

    jwt_signature = base64url_encode(signature)
    jwt = f"{jwt_unsigned}.{jwt_signature}"

    # Exchange JWT for access token
    response = requests.post(
        token_uri,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt,
        },
    )

    if response.status_code == 200:
        token_data = response.json()
        return token_data.get("access_token")
    else:
        raise Exception(f"Failed to get GCP access token: {response.text}")