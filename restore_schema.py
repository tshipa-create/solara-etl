import snowflake.connector
import os
import logging
import base64
import boto3
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def get_snowflake_connection():
    ssm_us = boto3.client("ssm", region_name="us-east-1")
    key_pem = ssm_us.get_parameter(Name="/snowflake/connection_private_key", WithDecryption=True)["Parameter"]["Value"]
    passphrase = ssm_us.get_parameter(Name="/snowflake/connection_passphrase", WithDecryption=True)["Parameter"]["Value"]
    
    p_key = serialization.load_pem_private_key(key_pem.encode(), password=passphrase.encode(), backend=default_backend())
    private_key_der = p_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_key_b64 = base64.b64encode(private_key_der).decode('utf-8')
    
    ssm_af = boto3.client("ssm", region_name="af-south-1")
    def fetch(key): 
        return ssm_af.get_parameter(Name=key, WithDecryption=True)["Parameter"]["Value"]
    
    conn = snowflake.connector.connect(
        account=fetch("/odoo_etl/SNOWFLAKE_ACCOUNT"),
        user=fetch("/odoo_etl/SNOWFLAKE_USER"),
        private_key=private_key_b64,
        warehouse=fetch("/odoo_etl/SNOWFLAKE_WAREHOUSE"),
        database=fetch("/odoo_etl/SNOWFLAKE_DATABASE"),
        role="SYSADMIN",
        login_timeout=15
    )
    return conn

def restore_schema():
    conn = get_snowflake_connection()
    cur = conn.cursor()
    
    try:
        schema_name = "ODS_SOLARA"
        user_name = "PLANET42_NEW_LIVE_USER_TABLEAU"
        db_name = cur.execute("SELECT CURRENT_DATABASE()").fetchone()[0]
        
        logger.info(f"Connected to database: {db_name}")
        
        logger.info(f"Granting permissions to user {user_name} on schema {schema_name}")
        cur.execute(f"GRANT USAGE ON SCHEMA {schema_name} TO USER {user_name}")
        cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema_name} TO USER {user_name}")
        cur.execute(f"GRANT SELECT ON FUTURE TABLES IN SCHEMA {schema_name} TO USER {user_name}")
        
        logger.info(f"✓ User {user_name} now has access to schema {schema_name}")
        
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    restore_schema()
