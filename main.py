import dlt
from dlt.sources.sql_database import sql_database
from dlt.destinations import snowflake
import boto3
import os
import urllib.parse
import logging
import sys
import datetime
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

import upper_naming

def setup_logging():
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    try:
        import watchtower
        logs_group = os.getenv('CLOUDWATCH_LOG_GROUP', '/aws/lambda/solara-etl')
        logs_stream = os.getenv('CLOUDWATCH_LOG_STREAM', 'etl-pipeline')
        
        cloudwatch_handler = watchtower.CloudWatchLogHandler(
            log_group=logs_group,
            stream_name=logs_stream,
            boto3_client=boto3.client('logs')
        )
        cloudwatch_handler.setLevel(logging.INFO)
        cloudwatch_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        cloudwatch_handler.setFormatter(cloudwatch_formatter)
        logger.addHandler(cloudwatch_handler)
    except ImportError:
        logger.warning("watchtower not installed; CloudWatch logging disabled")
    except Exception as e:
        logger.warning(f"Failed to setup CloudWatch logging: {e}")
    
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("dlt").setLevel(logging.DEBUG)
    logging.getLogger("dlt.load").setLevel(logging.DEBUG)
    
    return logger

logger = setup_logging()

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
        
        load_timestamp = datetime.datetime.now(datetime.timezone.utc)
        
        row_counts = {}
        rejected_rows = {}
        
        def add_timestamp(row):
            try:
                current_table = row.get('_table_name', 'unknown')
                if current_table not in row_counts:
                    row_counts[current_table] = 0
                row_counts[current_table] += 1
                
                row["LOAD_AT_TS_UTC"] = load_timestamp
                return row
            except Exception as e:
                current_table = row.get('_table_name', 'unknown') if isinstance(row, dict) else 'unknown'
                if current_table not in rejected_rows:
                    rejected_rows[current_table] = []
                rejected_rows[current_table].append({
                    'row': str(row)[:200],
                    'error': str(e)[:200]
                })
                logger.error(f"Failed to add timestamp to row from {current_table}: {e}", exc_info=True)
                raise

        logger.info("--- Inspecting Source Schema & Applying Fixes ---")

        for resource_name, resource in source.resources.items():
            logger.info(f"Processing table: {resource_name}")
            
            resource.add_map(add_timestamp)
            
            resource.write_disposition = "append"
            
            if resource.columns:
                column_hints = {
                    col_name: {"nullable": True} 
                    for col_name in resource.columns.keys()
                }
                resource.apply_hints(columns=column_hints)
                logger.info(f"Configured {resource_name}: append mode, all columns nullable")

    

        logger.info("--- Starting Extract & Load ---")
        
        info = pipeline.run(source, loader_file_format="csv")
        
        logger.info("--- PIPELINE COMPLETED ---")
        logger.info(f"Pipeline load info: {info}")
        
        logger.info("\n=== ROW PROCESSING SUMMARY ===")
        total_processed = sum(row_counts.values())
        logger.info(f"Total rows processed: {total_processed}")
        for table_name in sorted(row_counts.keys()):
            logger.info(f"  {table_name:40} | {row_counts[table_name]:8} rows processed")
        
        if rejected_rows:
            logger.warning("\n=== REJECTED ROWS ===")
            for table_name in sorted(rejected_rows.keys()):
                logger.warning(f"{table_name}: {len(rejected_rows[table_name])} rejected rows")
                for i, rejected in enumerate(rejected_rows[table_name][:3]):
                    logger.warning(f"  [{i+1}] Error: {rejected['error']}")
        
        if info.loads_ids:
            logger.info("\n=== LOAD SUMMARY ===")
            for load_id in info.loads_ids:
                logger.info(f"Load ID: {load_id}")
        
        if info.has_failed_jobs:
            logger.warning("Pipeline completed with failed jobs")
        else:
            logger.info("All jobs completed successfully")
        
    except Exception as e:
        logger.critical("Pipeline Crashed!", exc_info=True)

def lambda_handler(event, context):
    try:
        logger.info(f"ETL Pipeline triggered by EventBridge: {event}")
        run_pipeline()
        return {
            "statusCode": 200,
            "body": "ETL pipeline completed successfully"
        }
    except Exception as e:
        logger.error(f"ETL Pipeline failed: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "body": f"ETL pipeline failed: {str(e)}"
        }

if __name__ == "__main__":
    run_pipeline()