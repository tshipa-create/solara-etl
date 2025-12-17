import dlt
from dlt.sources.sql_database import sql_database
from dlt.destinations import snowflake
import boto3
import os
import urllib.parse
import logging
import sys
import datetime
import psycopg2 
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

import upper_naming

# --- CONFIGURATION ---
os.environ["DLT__NORMALIZE__ON_WRONG_TYPE"] = "fail"
os.environ["SCHEMA__NAMING"] = "upper_naming.UpperSnakeCase"

def setup_logging():
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(console_handler)
    
    try:
        import watchtower
        logs_group = os.getenv('CLOUDWATCH_LOG_GROUP', 'solara-etl-ec2')
        logs_stream = os.getenv('CLOUDWATCH_LOG_STREAM', 'production-run')
        
        cw_handler = watchtower.CloudWatchLogHandler(
            log_group=logs_group,
            stream_name=logs_stream,
            boto3_client=boto3.client('logs', region_name=os.getenv('AWS_REGION', 'us-east-1'))
        )
        cw_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        logger.addHandler(cw_handler)
    except ImportError:
        pass
    except Exception:
        pass
    
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("dlt").setLevel(logging.INFO)
    
    return logger

logger = setup_logging()
load_dotenv()

def get_postgres_uri():
    if not os.getenv('DB_USER') or not os.getenv('DB_PASSWORD'):
        raise ValueError("Missing DB_USER or DB_PASSWORD in .env")
    safe_password = urllib.parse.quote_plus(os.getenv('DB_PASSWORD'))
    return f"postgresql://{os.getenv('DB_USER')}:{safe_password}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME')}"

def get_snowflake_config_from_ssm():
    try:
        ssm_us = boto3.client("ssm", region_name="us-east-1")
        key_pem = ssm_us.get_parameter(Name="/snowflake/connection_private_key", WithDecryption=True)["Parameter"]["Value"]
        passphrase = ssm_us.get_parameter(Name="/snowflake/connection_passphrase", WithDecryption=True)["Parameter"]["Value"]

        p_key = serialization.load_pem_private_key(key_pem.encode(), password=passphrase.encode(), backend=default_backend())
        private_key_str = p_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode('utf-8')

        ssm_af = boto3.client("ssm", region_name="af-south-1")
        def fetch(key): return ssm_af.get_parameter(Name=key, WithDecryption=True)["Parameter"]["Value"]

        return {
            "host": fetch("/odoo_etl/SNOWFLAKE_ACCOUNT"),
            "username": fetch("/odoo_etl/SNOWFLAKE_USER"),
            "database": fetch("/odoo_etl/SNOWFLAKE_DATABASE"),
            "warehouse": fetch("/odoo_etl/SNOWFLAKE_WAREHOUSE"),
            "role": "SYSADMIN", 
            "private_key": private_key_str,
        }
    except Exception as e:
        logger.error("SSM Load Failed", exc_info=True)
        raise e

# --- DATA VERIFICATION FUNCTION ---
def verify_data_integrity(pipeline, pg_uri, table_names):
    logger.info("\n--- STARTING DATA VERIFICATION ---")
    
    try:
        # 1. Connect to Postgres (Source)
        pg_conn = psycopg2.connect(pg_uri)
        pg_cur = pg_conn.cursor()

        # 2. Connect to Snowflake (Destination) via dlt client
        with pipeline.sql_client() as snow_client:
            
            for table in table_names:
                logger.info(f"Checking table: {table}...")
                
                # A. VERIFY ROW COUNTS
                pg_cur.execute(f'SELECT COUNT(*) FROM public."{table}"')
                pg_count = pg_cur.fetchone()[0]

                # Convert table name to UpperSnakeCase for Snowflake check
                snow_table = table.upper()
                snow_schema = pipeline.dataset_name
                
                try:
                    res = snow_client.execute_sql(f'SELECT COUNT(*) FROM "{snow_schema}"."{snow_table}"')
                    snow_count = res[0][0]
                except Exception as e:
                    logger.error(f"[ERROR] Could not query Snowflake table {snow_table}: {e}")
                    continue

                if pg_count == snow_count:
                    logger.info(f"[OK] COUNT MATCH: {table} (PG: {pg_count} == SNOW: {snow_count})")
                else:
                    logger.error(f"[MISMATCH] COUNT ERROR: {table} (PG: {pg_count} != SNOW: {snow_count})")

                # B. VERIFY VARIANT TYPES
                # Find which columns in Postgres are JSON/JSONB
                pg_cur.execute(f"""
                    SELECT column_name FROM information_schema.columns 
                    WHERE table_schema = 'public' AND table_name = '{table}' 
                    AND data_type IN ('json', 'jsonb')
                """)
                pg_json_cols = [row[0] for row in pg_cur.fetchall()]

                if pg_json_cols:
                    # Check what these columns are in Snowflake
                    col_list_str = ", ".join([f"'{c.upper()}'" for c in pg_json_cols])
                    snow_type_query = f"""
                        SELECT column_name, data_type 
                        FROM information_schema.columns 
                        WHERE table_schema = '{snow_schema}' 
                        AND table_name = '{snow_table}'
                        AND column_name IN ({col_list_str})
                    """
                    snow_res = snow_client.execute_sql(snow_type_query)
                    snow_col_map = {r[0]: r[1] for r in snow_res}

                    for col in pg_json_cols:
                        col_upper = col.upper()
                        actual_type = snow_col_map.get(col_upper, "MISSING")
                        
                        if actual_type == 'VARIANT':
                            logger.info(f"[OK] TYPE MATCH: {col} -> VARIANT")
                        else:
                            logger.error(f"[ERROR] TYPE MISMATCH: {col} is {actual_type} (Expected VARIANT)")
                else:
                    logger.info(f"   (No JSON columns to verify for {table})")

        pg_conn.close()
        logger.info("--- VERIFICATION COMPLETE ---\n")

    except Exception as e:
        logger.error(f"Verification process failed: {e}", exc_info=True)

def run_pipeline():
    logger.info("--- STARTING ETL PIPELINE (EC2) ---")

    try:
        snow_creds = get_snowflake_config_from_ssm()
        pg_uri = get_postgres_uri()
        
        pipeline = dlt.pipeline(
            pipeline_name='solara_postgres_to_snowflake',
            destination=snowflake(credentials=snow_creds),
            dataset_name='ODS_SOLARA',
            progress="log"
        )

        source = sql_database(
            credentials=pg_uri,
            schema="public"
        )

        load_timestamp = datetime.datetime.now(datetime.timezone.utc)
        
        def add_timestamp(row):
            if isinstance(row, dict):
                row["LOAD_AT_TS_UTC"] = load_timestamp
            return row

        logger.info("--- Configuring Data Transformations ---")

        processed_tables = []

        for resource_name, resource in source.resources.items():
            processed_tables.append(resource_name)
            resource.add_map(add_timestamp)
            resource.write_disposition = "replace"
            resource.apply_hints(
                columns={col: {"nullable": True} for col in resource.columns.keys()}
            )

        logger.info("--- Starting Extract & Load ---")
        
        info = pipeline.run(source, loader_file_format="jsonl")
        logger.info(f"Pipeline Load Info: {info}")
        
        if info.has_failed_jobs:
            logger.error("!!! CRITICAL: Jobs failed! Check logs. !!!")
            sys.exit(1)
            
        verify_data_integrity(pipeline, pg_uri, processed_tables)
            
    except Exception as e:
        logger.critical("Pipeline Crashed!", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    run_pipeline()