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

def main():
    conn = get_snowflake_connection()
    cur = conn.cursor()
    
    logger.info("=" * 80)
    logger.info("CHECKING ALL SCHEMAS IN SNOWFLAKE")
    logger.info("=" * 80 + "\n")
    
    cur.execute("SELECT CURRENT_DATABASE()")
    db = cur.fetchone()[0]
    logger.info(f"Database: {db}\n")
    
    cur.execute("SHOW SCHEMAS")
    schemas = cur.fetchall()
    logger.info(f"Available Schemas:\n")
    for schema_row in schemas:
        logger.info(f"  - {schema_row[1]}")
    
    logger.info("\n" + "=" * 80)
    logger.info("CHECKING TABLE COUNTS IN EACH SCHEMA")
    logger.info("=" * 80 + "\n")
    
    for schema_row in schemas:
        schema_name = schema_row[1]
        try:
            cur.execute(f"""
                SELECT COUNT(*) FROM information_schema.tables 
                WHERE table_schema = '{schema_name}' 
                AND table_type = 'BASE TABLE'
            """)
            count = cur.fetchone()[0]
            if count > 0:
                logger.info(f"\n✓ {schema_name}: {count} tables")
                cur.execute(f"""
                    SELECT table_name, row_count 
                    FROM information_schema.tables 
                    WHERE table_schema = '{schema_name}'
                    AND table_type = 'BASE TABLE'
                    ORDER BY table_name
                    LIMIT 10
                """)
                tables = cur.fetchall()
                for table_name, row_count in tables:
                    logger.info(f"    {table_name}: {row_count} rows")
                if count > 10:
                    logger.info(f"    ... and {count - 10} more tables")
        except Exception as e:
            logger.warning(f"  Error checking {schema_name}: {e}")
    
    cur.close()
    conn.close()
    
    logger.info("\n" + "=" * 80)
    logger.info("GRANT PERMISSIONS TO USER: PLANET42_NEW_LIVE_USER_TABLEAU")
    logger.info("=" * 80 + "\n")
    
    conn = get_snowflake_connection()
    cur = conn.cursor()
    
    try:
        logger.info("Granting access to ODS_SOLARA schema and tables...")
        cur.execute("GRANT USAGE ON SCHEMA ODS_SOLARA TO USER PLANET42_NEW_LIVE_USER_TABLEAU")
        cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA ODS_SOLARA TO USER PLANET42_NEW_LIVE_USER_TABLEAU")
        cur.execute("GRANT SELECT ON FUTURE TABLES IN SCHEMA ODS_SOLARA TO USER PLANET42_NEW_LIVE_USER_TABLEAU")
        logger.info("✓ Permissions granted to user PLANET42_NEW_LIVE_USER_TABLEAU on schema ODS_SOLARA")
    except Exception as e:
        logger.warning(f"Error granting permissions: {e}")
    
    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
