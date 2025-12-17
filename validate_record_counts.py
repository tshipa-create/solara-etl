#!/usr/bin/env python3
import psycopg2
import snowflake.connector
import os
import logging
from dotenv import load_dotenv
import urllib.parse
import sys

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def get_postgres_connection():
    safe_password = urllib.parse.quote_plus(os.getenv('DB_PASSWORD'))
    conn = psycopg2.connect(
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT', '5432'),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD')
    )
    return conn

def get_snowflake_connection():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    import boto3
    import base64
    
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
    def fetch(key): return ssm_af.get_parameter(Name=key, WithDecryption=True)["Parameter"]["Value"]
    
    logger.info("Connecting to Snowflake...")
    
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

def get_postgres_tables():
    conn = get_postgres_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema='public' 
        AND table_type='BASE TABLE'
        ORDER BY table_name
    """)
    tables = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return tables

def get_record_count_postgres(table_name):
    conn = get_postgres_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM public.\"{table_name}\"")
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count

def get_record_count_snowflake(table_name, conn=None):
    if conn is None:
        return None
    
    cur = conn.cursor()
    snowflake_table = table_name.upper()
    try:
        cur.execute(f'SELECT COUNT(*) FROM "ODS_SOLARA"."{snowflake_table}"')
        count = cur.fetchone()[0]
    except Exception as e:
        logger.warning(f"Failed to get count for {snowflake_table}: {e}")
        count = None
    cur.close()
    return count

def main():
    logger.info("=" * 80)
    logger.info("RECORD COUNT VALIDATION: PostgreSQL vs Snowflake")
    logger.info("=" * 80)
    
    tables = get_postgres_tables()
    logger.info(f"\nFound {len(tables)} tables in PostgreSQL\n")
    
    logger.info("PostgreSQL Source Counts:")
    logger.info("-" * 80)
    
    total_postgres = 0
    postgres_counts = {}
    
    for table_name in tables:
        pg_count = get_record_count_postgres(table_name)
        total_postgres += pg_count
        postgres_counts[table_name] = pg_count
        logger.info(f"{table_name:40} | {pg_count:10}")
    
    logger.info("-" * 80)
    logger.info(f"TOTAL: {total_postgres:10}\n")
    
    logger.info("Attempting Snowflake connection...")
    sf_conn = None
    try:
        sf_conn = get_snowflake_connection()
        logger.info("✓ Connected to Snowflake")
        
        total_snowflake = 0
        snowflake_counts = {}
        
        for table_name in tables:
            sf_count = get_record_count_snowflake(table_name, sf_conn)
            if sf_count is not None:
                total_snowflake += sf_count
                snowflake_counts[table_name] = sf_count
        
        logger.info("\nSnowflake Target Counts:")
        logger.info("-" * 80)
        
        mismatches = []
        for table_name in tables:
            pg_count = postgres_counts[table_name]
            sf_count = snowflake_counts.get(table_name)
            
            status = "✓" if pg_count == sf_count else "✗"
            sf_str = str(sf_count) if sf_count is not None else "NOT FOUND"
            
            logger.info(f"{status} {table_name:40} | PG: {pg_count:10} | SF: {sf_str:10}")
            
            if pg_count != sf_count:
                diff = sf_count - pg_count if sf_count is not None else 0
                mismatches.append({
                    'table': table_name,
                    'postgres': pg_count,
                    'snowflake': sf_count,
                    'diff': diff
                })
        
        logger.info("-" * 80)
        logger.info(f"TOTALS: PostgreSQL: {total_postgres:10} | Snowflake: {total_snowflake:10}\n")
        
        if mismatches:
            logger.info(f"⚠️  FOUND {len(mismatches)} MISMATCHES:\n")
            for m in mismatches:
                logger.info(f"  {m['table']:40} | Diff: {m['diff']:+10}")
        else:
            logger.info("✓ All record counts match!")
    except Exception as e:
        logger.warning(f"\n✗ Couldn't connect to Snowflake: {e}")
        logger.info("\n✓ PostgreSQL validation complete (Snowflake connection failed)")
        logger.info("  You can manually verify Snowflake counts in the console")
    finally:
        if sf_conn:
            try:
                sf_conn.close()
            except:
                pass

if __name__ == '__main__':
    main()
