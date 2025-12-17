#!/usr/bin/env python3
import subprocess
import sys
import psycopg2
import snowflake.connector
import os
import logging
from dotenv import load_dotenv
import base64
import boto3
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def run_command(cmd, name):
    logger.info(f"\n{'=' * 80}")
    logger.info(f"STEP: {name}")
    logger.info(f"{'=' * 80}\n")
    result = subprocess.run(cmd, shell=True)
    if result.returncode != 0:
        logger.error(f"✗ {name} failed with exit code {result.returncode}")
        return False
    return True

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

def get_postgres_connection():
    return psycopg2.connect(
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT', '5432'),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD')
    )

def analyze_duplicates():
    logger.info(f"\n{'=' * 80}")
    logger.info("STEP: Checking for Duplicates in Snowflake")
    logger.info(f"{'=' * 80}\n")
    
    try:
        conn = get_snowflake_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'PLANET42_NEW_LIVE_USER_TABLEAU'
            AND table_name NOT LIKE '%LOAD_METADATA%'
        """)
        tables = [row[0] for row in cur.fetchall()]
        
        logger.info(f"Found {len(tables)} tables to check\n")
        
        duplicates_found = False
        for table in tables:
            try:
                cur.execute(f"""
                    SELECT COUNT(*) as total_rows,
                           COUNT(DISTINCT ROW_NUMBER() OVER (ORDER BY (SELECT NULL))) as unique_rows
                    FROM "{table}"
                    LIMIT 1
                """)
                result = cur.fetchone()
                
                if result and result[0] > 0:
                    total = result[0]
                    
                    cur.execute(f"""
                        SELECT COUNT(*) FROM (
                            SELECT * FROM "{table}"
                            QUALIFY ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) > 1
                        )
                    """)
                    dup_count = cur.fetchone()[0] if cur.fetchone() else 0
                    
                    if dup_count > 0:
                        logger.warning(f"✗ {table}: {total} total rows, {dup_count} potential duplicates")
                        duplicates_found = True
                    else:
                        logger.info(f"✓ {table}: {total} rows, no duplicates detected")
            except Exception as e:
                logger.warning(f"  Could not check {table}: {e}")
        
        if not duplicates_found:
            logger.info("\n✓ No duplicates detected")
        
        cur.close()
        conn.close()
        
    except Exception as e:
        logger.error(f"✗ Duplicate check failed: {e}", exc_info=True)

def main():
    logger.info("\n" + "=" * 80)
    logger.info("ETL COMPREHENSIVE ANALYSIS")
    logger.info("=" * 80)
    
    if not run_command("python restore_schema.py", "Restore Schema & Permissions"):
        sys.exit(1)
    
    if not run_command("python main.py", "Run ETL Pipeline"):
        logger.warning("⚠️  ETL completed with warnings or errors")
    
    if not run_command("python validate_record_counts.py", "Validate Record Counts"):
        logger.warning("⚠️  Record count validation had issues")
    
    analyze_duplicates()
    
    logger.info("\n" + "=" * 80)
    logger.info("ETL ANALYSIS COMPLETE")
    logger.info("=" * 80)
    logger.info("\nReview the output above to verify:")
    logger.info("  1. Schema was restored successfully")
    logger.info("  2. ETL pipeline completed")
    logger.info("  3. Record counts match between PostgreSQL and Snowflake")
    logger.info("  4. No duplicate records were detected")
    logger.info("=" * 80 + "\n")

if __name__ == "__main__":
    main()
