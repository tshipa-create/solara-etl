import dlt
from dlt.sources.sql_database import sql_database
from dlt.destinations import snowflake
import boto3
import os
import urllib.parse
import logging
import logging.handlers
import sys
import datetime
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# --- IMPORT FOR UPPERCASE ---
import upper_naming
# -------------------------------------

# 1. SETUP LOGGING
os.makedirs('logs', exist_ok=True)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console_handler.setFormatter(console_formatter)

file_handler = logging.handlers.TimedRotatingFileHandler(
    'logs/solara_to_snowflake.log',
    when='midnight',
    interval=1,
    backupCount=30
)
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler.setFormatter(file_formatter)

logger.addHandler(console_handler)
logger.addHandler(file_handler)


# Silence noisy libraries
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("dlt").setLevel(logging.INFO)

load_dotenv()

# 2. SOURCE (Postgres)
def get_postgres_uri():
    if not os.getenv('DB_USER') or not os.getenv('DB_PASSWORD'):
        logger.error("Missing DB_USER or DB_PASSWORD in .env")
        raise ValueError("Missing Postgres Credentials")

    safe_password = urllib.parse.quote_plus(os.getenv('DB_PASSWORD'))
    return f"postgresql://{os.getenv('DB_USER')}:{safe_password}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME')}"

# 3. DESTINATION (Snowflake via SSM)
def get_snowflake_config_from_ssm():
    logger.info("Fetching Snowflake secrets from SSM...")
    try:
        ssm_us = boto3.client("ssm", region_name="us-east-1")
        key_pem = ssm_us.get_parameter(Name="/snowflake/connection_private_key", WithDecryption=True)["Parameter"]["Value"]
        passphrase = ssm_us.get_parameter(Name="/snowflake/connection_passphrase", WithDecryption=True)["Parameter"]["Value"]

        p_key = serialization.load_pem_private_key(key_pem.encode(), password=passphrase.encode(), backend=default_backend())
        # Decode bytes to string for dlt
        private_key_str = p_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode('utf-8')

        ssm_af = boto3.client("ssm", region_name="af-south-1")
        def fetch(key): return ssm_af.get_parameter(Name=key, WithDecryption=True)["Parameter"]["Value"]

        host_raw = fetch("/odoo_etl/SNOWFLAKE_ACCOUNT")

        return {
            "host": host_raw,
            "username": fetch("/odoo_etl/SNOWFLAKE_USER"),
            "database": fetch("/odoo_etl/SNOWFLAKE_DATABASE"),
            "warehouse": fetch("/odoo_etl/SNOWFLAKE_WAREHOUSE"),
            "role": "SYSADMIN", 
            "private_key": private_key_str,
        }
    except Exception as e:
        logger.error("SSM Load Failed", exc_info=True)
        raise e

# 4. MAIN EXECUTION
def run_pipeline():
    logger.info("--- STARTING ETL PIPELINE ---")
    
    # FORCE UPPERCASE
    os.environ["SCHEMA__NAMING"] = "upper_naming.UpperSnakeCase"

    try:
        snow_creds = get_snowflake_config_from_ssm()
        destination = snowflake(credentials=snow_creds)
        pg_uri = get_postgres_uri()
        
        pipeline = dlt.pipeline(
            pipeline_name='solara_postgres_to_snowflake',
            destination=destination,
            dataset_name='ODS_SOLARA', # Target Schema
        )

        source = sql_database(
            credentials=pg_uri,
            schema="public"
        )

        # --- DYNAMIC TRANSFORMATION & SCHEMA FIX ---
        
        # Define the timestamp adder function
        def add_timestamp(row):
            row["LOAD_AT_TS_UTC"] = datetime.datetime.now(datetime.timezone.utc)
            return row

        logger.info("--- Inspecting Source Schema & Applying Fixes ---")

        # Iterate through every table (resource) found in the Postgres source
        for resource_name, resource in source.resources.items():
            
            # 1. Add the LOAD_AT_TS_UTC to the data stream
            resource.add_map(add_timestamp)
            
            # 2. Set write_disposition to "replace" to force table recreation with nullable columns
            resource.write_disposition = "replace"
            
            # 3. Apply hints to make EVERY column nullable
            # This ensures columns are created as NULLABLE in Snowflake
            if resource.columns:
                column_hints = {
                    col_name: {"nullable": True} 
                    for col_name in resource.columns.keys()
                }
                resource.apply_hints(columns=column_hints)
                logger.info(f"Updated schema for table: {resource_name} (All columns set to Nullable, write_disposition=replace)")

    

        logger.info("--- Starting Extract & Load ---")
        
        # Run pipeline (tables will be recreated due to write_disposition="replace" set on resources)
        info = pipeline.run(source, loader_file_format="csv")
        
        logger.info("--- PIPELINE COMPLETED ---")
        logger.info(info)
        
    except Exception as e:
        logger.critical("Pipeline Crashed!", exc_info=True)

if __name__ == "__main__":
    run_pipeline()