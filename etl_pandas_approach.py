#!/usr/bin/env python3
import pandas as pd
import psycopg2
import snowflake.connector
import json
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

def serialize_json_columns(df):
    """Convert dict/list columns to JSON strings"""
    for col in df.columns:
        if df[col].dtype == 'object':
            def to_json(val):
                if isinstance(val, (dict, list)):
                    return json.dumps(val)
                return val
            df[col] = df[col].apply(to_json)
    return df

def load_table_to_snowflake(table_name, df, sf_conn):
    """Load DataFrame to Snowflake with REPLACE mode"""
    if df.empty:
        logger.info(f"  Skipping {table_name}: no data")
        return 0
    
    try:
        table_upper = table_name.upper()
        
        cur = sf_conn.cursor()
        cur.execute(f'DROP TABLE IF EXISTS ODS_SOLARA.{table_upper}')
        
        df.to_sql(
            table_upper,
            con=sf_conn,
            schema='ODS_SOLARA',
            if_exists='replace',
            index=False,
            method='multi',
            chunksize=1000
        )
        
        cur.execute(f'SELECT COUNT(*) FROM ODS_SOLARA.{table_upper}')
        count = cur.fetchone()[0]
        cur.close()
        
        logger.info(f"  ✓ {table_name}: {count} records loaded")
        return count
        
    except Exception as e:
        logger.error(f"  ✗ {table_name}: {e}")
        return 0

def run_etl():
    logger.info("\n" + "=" * 80)
    logger.info("PANDAS-BASED ETL: PostgreSQL → Snowflake")
    logger.info("=" * 80 + "\n")
    
    postgres_conn = get_postgres_connection()
    snowflake_conn = get_snowflake_connection()
    
    tables = get_postgres_tables()
    logger.info(f"Found {len(tables)} tables to load\n")
    
    total_records = 0
    
    for table_name in tables:
        try:
            df = pd.read_sql(f'SELECT * FROM public."{table_name}"', postgres_conn)
            
            if df.empty:
                logger.info(f"  {table_name}: 0 records (skipped)")
                continue
            
            df = serialize_json_columns(df)
            
            count = load_table_to_snowflake(table_name, df, snowflake_conn)
            total_records += count
            
        except Exception as e:
            logger.error(f"  {table_name}: {e}")
    
    postgres_conn.close()
    snowflake_conn.close()
    
    logger.info(f"\n{'=' * 80}")
    logger.info(f"Total records loaded: {total_records}")
    logger.info(f"{'=' * 80}\n")

if __name__ == "__main__":
    run_etl()
