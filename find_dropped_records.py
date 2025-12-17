#!/usr/bin/env python3

import psycopg2
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

def get_postgres_tables():
    conn = get_postgres_connection()
    cur = conn.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
    tables = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return tables

def get_table_pkey(table_name):
    conn = get_postgres_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT column_name FROM information_schema.constraint_column_usage 
            WHERE table_name = %s 
            AND constraint_name IN (
                SELECT constraint_name FROM information_schema.table_constraints 
                WHERE table_name = %s AND constraint_type = 'PRIMARY KEY'
            ) LIMIT 1
        """, (table_name, table_name))
        result = cur.fetchone()
        if result:
            pk = result[0]
        else:
            cur.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = %s LIMIT 1", (table_name,))
            result = cur.fetchone()
            pk = result[0] if result else None
        cur.close()
        conn.close()
        return pk
    except Exception as e:
        logger.warning(f"Could not find PK for {table_name}: {e}")
        cur.close()
        conn.close()
        return None

def get_pg_count(table_name):
    try:
        conn = get_postgres_connection()
        cur = conn.cursor()
        cur.execute(f'SELECT COUNT(*) FROM public."{table_name}"')
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception as e:
        logger.warning(f"Error counting {table_name}: {e}")
        return 0

def get_sf_count(table_name, sf_conn):
    try:
        cur = sf_conn.cursor()
        cur.execute(f'SELECT COUNT(*) FROM "ODS_SOLARA"."{table_name.upper()}"')
        count = cur.fetchone()[0]
        cur.close()
        return count
    except Exception as e:
        logger.warning(f"Error counting {table_name} in Snowflake: {e}")
        return 0

def get_missing_ids(table_name, pk_col):
    if not pk_col:
        return []
    try:
        conn = get_postgres_connection()
        cur = conn.cursor()
        cur.execute(f'SELECT {pk_col}::text FROM public."{table_name}" ORDER BY {pk_col}')
        pg_ids = set(row[0] for row in cur.fetchall())
        cur.close()
        conn.close()
        return pg_ids
    except Exception as e:
        logger.warning(f"Could not get IDs from {table_name}: {e}")
        return set()

def get_sf_ids(table_name, pk_col, sf_conn):
    if not pk_col:
        return set()
    try:
        cur = sf_conn.cursor()
        cur.execute(f'SELECT {pk_col.upper()}::text FROM "ODS_SOLARA"."{table_name.upper()}" ORDER BY {pk_col.upper()}')
        sf_ids = set(row[0] for row in cur.fetchall())
        cur.close()
        return sf_ids
    except Exception as e:
        logger.warning(f"Could not get IDs from {table_name} in Snowflake: {e}")
        return set()

def analyze_sequential_gaps(missing_ids):
    if not missing_ids:
        return None
    missing_nums = sorted([int(x) for x in missing_ids if x.isdigit()])
    if not missing_nums:
        return None
    
    gaps = []
    start = missing_nums[0]
    end = missing_nums[0]
    for num in missing_nums[1:]:
        if num == end + 1:
            end = num
        else:
            gaps.append((start, end))
            start = num
            end = num
    gaps.append((start, end))
    return gaps

def main():
    logger.info("=" * 80)
    logger.info("IDENTIFYING DROPPED RECORDS")
    logger.info("=" * 80)
    
    tables = get_postgres_tables()
    sf_conn = get_snowflake_connection()
    
    total_pg = 0
    total_sf = 0
    total_dropped = 0
    dropped_details = {}
    
    for table_name in tables:
        pk_col = get_table_pkey(table_name)
        pg_count = get_pg_count(table_name)
        sf_count = get_sf_count(table_name, sf_conn)
        
        total_pg += pg_count
        total_sf += sf_count
        
        dropped = pg_count - sf_count
        total_dropped += dropped
        
        if dropped > 0:
            pct = dropped / pg_count * 100 if pg_count > 0 else 0
            logger.info(f"\n{table_name}:")
            logger.info(f"  PostgreSQL: {pg_count:8} | Snowflake: {sf_count:8} | DROPPED: {dropped:6} ({pct:.2f}%)")
            
            if pk_col:
                pg_ids = get_missing_ids(table_name, pk_col)
                sf_ids = get_sf_ids(table_name, pk_col, sf_conn)
                missing_ids = pg_ids - sf_ids
                
                dropped_details[table_name] = {
                    'dropped_count': len(missing_ids),
                    'sample_ids': sorted(list(missing_ids))[:10],
                    'all_missing': missing_ids
                }
                
                if len(missing_ids) <= 10:
                    logger.info(f"  Missing IDs: {sorted(list(missing_ids))}")
                else:
                    gaps = analyze_sequential_gaps(missing_ids)
                    if gaps and len(gaps) <= 5:
                        logger.info(f"  Missing ID ranges:")
                        for start, end in gaps:
                            if start == end:
                                logger.info(f"    {start}")
                            else:
                                logger.info(f"    {start}-{end} ({end-start+1} records)")
                    else:
                        logger.info(f"  Missing IDs (first 10): {sorted(list(missing_ids))[:10]}")
            else:
                logger.info(f"  Could not determine primary key")
    
    sf_conn.close()
    
    logger.info("\n" + "=" * 80)
    logger.info("SUMMARY")
    logger.info("=" * 80)
    logger.info(f"PostgreSQL Total: {total_pg:8}")
    logger.info(f"Snowflake Total:  {total_sf:8}")
    logger.info(f"Total Dropped:    {total_dropped:8} ({total_dropped/total_pg*100:.2f}%)")
    
    if dropped_details:
        logger.info("\nTables with Drops (sorted by impact):")
        for table in sorted(dropped_details.keys(), key=lambda t: dropped_details[t]['dropped_count'], reverse=True):
            detail = dropped_details[table]
            logger.info(f"  {table:40} | {detail['dropped_count']:6} records")

if __name__ == "__main__":
    main()
