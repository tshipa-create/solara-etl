#!/usr/bin/env python3
import psycopg2
import snowflake.connector
import json
import os
import logging
import base64
import boto3
import datetime
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

def get_column_info(conn, table_name):
    """Get column names and types from PostgreSQL"""
    cur = conn.cursor()
    cur.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
    """, (table_name,))
    cols = cur.fetchall()
    cur.close()
    return cols

def serialize_value(value, data_type):
    """Convert PostgreSQL value to Snowflake-compatible format"""
    if value is None:
        return None
    
    if data_type in ['jsonb', 'json']:
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return value
    
    if data_type == 'inet':
        return str(value)
    
    if data_type == 'bytea':
        if isinstance(value, bytes):
            return value.hex()
        return value
    
    return value

def drop_and_recreate_table(sf_cur, table_name, columns):
    """Drop table and create new one in Snowflake"""
    table_upper = table_name.upper()
    
    sf_cur.execute(f"DROP TABLE IF EXISTS ODS_SOLARA.{table_upper}")
    
    col_defs = []
    for col_name, col_type in columns:
        col_upper = col_name.upper()
        if col_type in ['jsonb', 'json']:
            sf_type = 'VARIANT'
        elif col_type == 'inet':
            sf_type = 'VARCHAR'
        elif col_type == 'bytea':
            sf_type = 'VARCHAR'
        else:
            sf_type = 'VARCHAR'
        
        col_defs.append(f"{col_upper} {sf_type}")
    
    create_sql = f"CREATE TABLE ODS_SOLARA.{table_upper} ({', '.join(col_defs)})"
    sf_cur.execute(create_sql)

def load_table_in_batches(table_name, pg_conn, sf_conn, batch_size=5000):
    """Load table in batches with proper serialization"""
    pg_cur = pg_conn.cursor()
    sf_cur = sf_conn.cursor()
    
    columns = get_column_info(pg_conn, table_name)
    col_names = [col[0] for col in columns]
    
    pg_cur.execute(f'SELECT COUNT(*) FROM public."{table_name}"')
    total_count = pg_cur.fetchone()[0]
    
    if total_count == 0:
        logger.info(f"  {table_name}: 0 records")
        return 0
    
    try:
        drop_and_recreate_table(sf_cur, table_name, columns)
        
        table_upper = table_name.upper()
        loaded = 0
        offset = 0
        
        while offset < total_count:
            pg_cur.execute(f"""
                SELECT * FROM public."{table_name}"
                ORDER BY {col_names[0]}
                LIMIT %s OFFSET %s
            """, (batch_size, offset))
            
            rows = pg_cur.fetchall()
            if not rows:
                break
            
            serialized_rows = []
            for row in rows:
                serialized_row = []
                for value, (col_name, col_type) in zip(row, columns):
                    serialized_row.append(serialize_value(value, col_type))
                serialized_rows.append(serialized_row)
            
            placeholders = ', '.join(['%s'] * len(col_names))
            insert_sql = f"INSERT INTO ODS_SOLARA.{table_upper} ({', '.join([c.upper() for c in col_names])}) VALUES ({placeholders})"
            
            sf_cur.executemany(insert_sql, serialized_rows)
            loaded += len(serialized_rows)
            offset += batch_size
            
            logger.info(f"  {table_name}: {loaded}/{total_count} records...")
        
        logger.info(f"  ✓ {table_name}: {loaded} records loaded")
        return loaded
        
    except Exception as e:
        logger.error(f"  ✗ {table_name}: {e}", exc_info=True)
        return 0
    finally:
        pg_cur.close()
        sf_cur.close()

def run_etl():
    logger.info("\n" + "=" * 80)
    logger.info("CUSTOM ETL: PostgreSQL → Snowflake (100% Record Sync)")
    logger.info("=" * 80 + "\n")
    
    pg_conn = get_postgres_connection()
    sf_conn = get_snowflake_connection()
    
    tables = get_postgres_tables()
    logger.info(f"Found {len(tables)} tables to load\n")
    
    total_records = 0
    
    for table_name in tables:
        count = load_table_in_batches(table_name, pg_conn, sf_conn)
        total_records += count
    
    pg_conn.close()
    sf_conn.close()
    
    logger.info(f"\n{'=' * 80}")
    logger.info(f"Total records loaded: {total_records}")
    logger.info(f"{'=' * 80}\n")

if __name__ == "__main__":
    run_etl()
