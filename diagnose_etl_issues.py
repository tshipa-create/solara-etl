#!/usr/bin/env python3
import psycopg2
import snowflake.connector
import os
import logging
import base64
import boto3
import json
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def get_postgres_connection():
    return psycopg2.connect(
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT', '5432'),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD')
    )

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

def check_empty_tables():
    logger.info(f"\n{'=' * 80}")
    logger.info("ISSUE #2: Why aren't empty/missing tables created?")
    logger.info(f"{'=' * 80}\n")
    
    conn = get_postgres_connection()
    cur = conn.cursor()
    
    missing_in_sf = [
        'p42_inspection', 'p42_salarydata', 'p42_scoringmaxrentconfig',
        'p42_scoringofferdecision', 'p42_scoringresult', 'p42_scoringversions',
        'p42_service', 'p42_signing', 'p42_validation',
        'payments_collections', 'payments_customerinvoice', 'payments_customerinvoiceline',
        'payments_customerrecurringinvoice', 'payments_debicheck_mandates', 'payments_payment_methods',
        'public_holidays'
    ]
    
    logger.info("PostgreSQL Record Counts for Missing Tables:\n")
    for table in missing_in_sf:
        try:
            cur.execute(f'SELECT COUNT(*) FROM public."{table}"')
            count = cur.fetchone()[0]
            
            cur.execute(f"""
                SELECT column_name, data_type 
                FROM information_schema.columns 
                WHERE table_name = %s 
                ORDER BY ordinal_position 
                LIMIT 5
            """, (table,))
            cols = cur.fetchall()
            col_str = ", ".join([f"{c[0]}({c[1]})" for c in cols])
            
            logger.info(f"  {table:40} | COUNT: {count:8} | COLS: {col_str}...")
            
            if count > 0:
                logger.warning(f"    ⚠️  {table} has {count} records but doesn't exist in Snowflake!")
            
        except Exception as e:
            logger.warning(f"  {table:40} | ERROR: {e}")
    
    cur.close()
    conn.close()

def check_dropped_records():
    logger.info(f"\n{'=' * 80}")
    logger.info("ISSUE #1: Why are records being dropped?")
    logger.info(f"{'=' * 80}\n")
    
    conn = get_postgres_connection()
    cur = conn.cursor()
    
    problem_tables = {
        'auditlog_logentry': (342679, 342766),
        'p42_vehicledata': (59656, 59680)
    }
    
    for table, (start_id, end_id) in problem_tables.items():
        logger.info(f"\nAnalyzing dropped records in {table}:")
        logger.info(f"  Missing IDs: {start_id}-{end_id}\n")
        
        cur.execute(f"""
            SELECT id
            FROM public."{table}"
            WHERE id BETWEEN %s AND %s
            LIMIT 3
        """, (start_id, end_id))
        
        rows = cur.fetchall()
        if rows:
            logger.info(f"  Sample dropped records found in PostgreSQL:")
            for row in rows:
                logger.info(f"    ID: {row[0]}")
        else:
            logger.info(f"  No records found in PostgreSQL for dropped IDs (these may not exist)")
        
        cur.execute(f"""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = %s
            AND data_type IN ('jsonb', 'json', 'bytea', 'USER-DEFINED')
            ORDER BY ordinal_position
        """, (table,))
        
        complex_cols = cur.fetchall()
        if complex_cols:
            logger.info(f"  Complex column types:")
            for col_name, col_type in complex_cols:
                logger.info(f"    {col_name}: {col_type}")
    
    cur.close()
    conn.close()

def test_json_serialization():
    logger.info(f"\n{'=' * 80}")
    logger.info("Testing JSON serialization function")
    logger.info(f"{'=' * 80}\n")
    
    def serialize_json_for_variant(row):
        if not isinstance(row, dict):
            return row
        
        for col_name, value in list(row.items()):
            if value is None:
                continue
            
            col_type = type(value).__name__
            
            if col_type in ['dict', 'list']:
                row[col_name] = json.dumps(value)
        
        return row
    
    test_rows = [
        {'id': 1, 'data': {'nested': 'value'}},
        {'id': 2, 'data': None},
        {'id': 3, 'data': ['a', 'b', 'c']},
        {'id': 4, 'data': {'very': {'deeply': {'nested': {'structure': 'here'}}}}},
    ]
    
    logger.info("Testing serialization on sample rows:\n")
    for row in test_rows:
        try:
            result = serialize_json_for_variant(row.copy())
            logger.info(f"  Input:  {row}")
            logger.info(f"  Output: {result}")
            logger.info()
        except Exception as e:
            logger.error(f"  ERROR serializing {row}: {e}\n")

def check_snowflake_schema():
    logger.info(f"\n{'=' * 80}")
    logger.info("Checking Snowflake Schema & Permissions")
    logger.info(f"{'=' * 80}\n")
    
    try:
        conn = get_snowflake_connection()
        cur = conn.cursor()
        
        cur.execute("SELECT CURRENT_SCHEMA()")
        schema = cur.fetchone()[0]
        logger.info(f"Current schema: {schema}\n")
        
        cur.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'PLANET42_NEW_LIVE_USER_TABLEAU'
            ORDER BY table_name
        """)
        tables = cur.fetchall()
        logger.info(f"Tables in PLANET42_NEW_LIVE_USER_TABLEAU: {len(tables)}")
        for t in tables[:5]:
            logger.info(f"  - {t[0]}")
        if len(tables) > 5:
            logger.info(f"  ... and {len(tables) - 5} more")
        
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to check Snowflake: {e}", exc_info=True)

def main():
    logger.info("\n" + "=" * 80)
    logger.info("ETL DIAGNOSTIC REPORT")
    logger.info("=" * 80)
    
    check_empty_tables()
    check_dropped_records()
    test_json_serialization()
    check_snowflake_schema()
    
    logger.info(f"\n{'=' * 80}")
    logger.info("RECOMMENDATIONS:")
    logger.info(f"{'=' * 80}\n")
    logger.info("1. Check if missing tables have zero records in PostgreSQL")
    logger.info("   If yes: dlt won't create empty tables (expected behavior)")
    logger.info("   If no: investigate why they failed to load\n")
    logger.info("2. For dropped records: Check main.py ETL logs for serialization errors\n")
    logger.info("3. Add row-level error logging to identify exactly which rows fail\n")
    logger.info("=" * 80 + "\n")

if __name__ == "__main__":
    main()
