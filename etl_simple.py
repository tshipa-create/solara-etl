#!/usr/bin/env python3
import pandas as pd
import psycopg2
import snowflake.connector
from snowflake.connector.pandas_tools import write_pandas
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

def load_table(table_name, pg_conn, sf_conn):
    """Load single table from PostgreSQL to Snowflake"""
    try:
        pg_cur = pg_conn.cursor()
        pg_cur.execute(f"""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = %s
            ORDER BY ordinal_position
        """, (table_name,))
        cols = pg_cur.fetchall()
        pg_cur.close()
        
        json_cols = [col[0] for col in cols if col[1] in ['jsonb', 'json']]
        
        df = pd.read_sql(f'SELECT * FROM public."{table_name}"', pg_conn)
        
        if df.empty:
            logger.info(f"  {table_name}: 0 records")
            return 0
        
        for json_col in json_cols:
            if json_col in df.columns:
                df[json_col] = df[json_col].apply(
                    lambda x: json.dumps(x) if isinstance(x, (dict, list)) else x
                )
        
        success, nrows, nchunks, output = write_pandas(
            sf_conn,
            df,
            table_name.upper(),
            schema='ODS_SWF_SA_RAW',
            overwrite=True
        )
        
        if success:
            logger.info(f"  ✓ {table_name}: {nrows} records")
            return nrows
        else:
            logger.error(f"  ✗ {table_name}: write_pandas failed")
            return 0
            
    except Exception as e:
        logger.error(f"  ✗ {table_name}: {e}")
        return 0

def truncate_schema(sf_conn):
    """Truncate all tables in ODS_SWF_SA_RAW schema"""
    logger.info("Truncating ODS_SWF_SA_RAW schema...\n")
    cur = sf_conn.cursor()
    
    try:
        cur.execute("""
            SELECT table_name FROM information_schema.tables 
            WHERE table_schema = 'ODS_SWF_SA_RAW'
            AND table_type = 'BASE TABLE'
        """)
        tables = cur.fetchall()
        
        for table_row in tables:
            table_name = table_row[0]
            cur.execute(f"TRUNCATE TABLE ODS_SWF_SA_RAW.{table_name}")
            logger.info(f"  Truncated: {table_name}")
        
        if tables:
            logger.info()
    except Exception as e:
        logger.warning(f"Error truncating schema: {e}")
    finally:
        cur.close()

def run_etl():
    logger.info("\n" + "=" * 80)
    logger.info("Simple ETL: PostgreSQL → ODS_SWF_SA_RAW (Snowflake)")
    logger.info("=" * 80 + "\n")
    
    pg_conn = get_postgres_connection()
    sf_conn = get_snowflake_connection()
    
    truncate_schema(sf_conn)
    
    tables = get_postgres_tables()
    logger.info(f"Loading {len(tables)} tables\n")
    
    total = 0
    for table_name in tables:
        count = load_table(table_name, pg_conn, sf_conn)
        total += count
    
    pg_conn.close()
    sf_conn.close()
    
    logger.info(f"\n{'=' * 80}")
    logger.info(f"Total: {total} records")
    logger.info(f"{'=' * 80}\n")

if __name__ == "__main__":
    run_etl()
