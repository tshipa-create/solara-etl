#!/usr/bin/env python3
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

def clean_schema(sf_conn):
    logger.info("Cleaning ODS_SWF_SA_RAW (drop all tables)...\n")
    cur = sf_conn.cursor()
    
    try:
        cur.execute("""
            SELECT table_name FROM information_schema.tables 
            WHERE table_schema = 'ODS_SWF_SA_RAW'
        """)
        tables = cur.fetchall()
        
        for table_row in tables:
            cur.execute(f"DROP TABLE ODS_SWF_SA_RAW.{table_row[0]}")
            logger.info(f"  Dropped: {table_row[0]}")
        
        if tables:
            logger.info()
    except Exception as e:
        logger.warning(f"Error: {e}")
    finally:
        cur.close()

def map_postgres_to_snowflake_type(pg_type):
    """Map PostgreSQL types to Snowflake types"""
    type_map = {
        'integer': 'INTEGER',
        'bigint': 'BIGINT',
        'smallint': 'INTEGER',
        'numeric': 'DECIMAL',
        'real': 'FLOAT',
        'double precision': 'DOUBLE',
        'boolean': 'BOOLEAN',
        'text': 'VARCHAR',
        'character varying': 'VARCHAR',
        'varchar': 'VARCHAR',
        'char': 'VARCHAR',
        'date': 'DATE',
        'timestamp': 'TIMESTAMP',
        'timestamp without time zone': 'TIMESTAMP',
        'timestamp with time zone': 'TIMESTAMP_TZ',
        'jsonb': 'VARIANT',
        'json': 'VARIANT',
        'bytea': 'VARCHAR',
        'inet': 'VARCHAR',
    }
    return type_map.get(pg_type.lower(), 'VARCHAR')

def create_table(table_name, pg_conn, sf_conn):
    """Create table in Snowflake based on PostgreSQL schema"""
    pg_cur = pg_conn.cursor()
    
    pg_cur.execute(f"""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
    """, (table_name,))
    cols = pg_cur.fetchall()
    pg_cur.close()
    
    col_defs = []
    for col_name, col_type in cols:
        sf_type = map_postgres_to_snowflake_type(col_type)
        col_defs.append(f"{col_name.upper()} {sf_type}")
    
    sf_cur = sf_conn.cursor()
    create_sql = f"CREATE TABLE IF NOT EXISTS ODS_SWF_SA_RAW.{table_name.upper()} ({', '.join(col_defs)})"
    sf_cur.execute(create_sql)
    sf_cur.close()

def load_table(table_name, pg_conn, sf_conn):
    try:
        pg_cur = pg_conn.cursor()
        
        pg_cur.execute(f"SELECT COUNT(*) FROM public.\"{table_name}\"")
        count = pg_cur.fetchone()[0]
        
        if count == 0:
            logger.info(f"  {table_name}: 0 records")
            return 0
        
        pg_cur.execute(f"""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = %s
            ORDER BY ordinal_position
        """, (table_name,))
        cols = pg_cur.fetchall()
        col_names = [col[0] for col in cols]
        
        create_table(table_name, pg_conn, sf_conn)
        
        pg_cur.execute(f"SELECT * FROM public.\"{table_name}\"")
        rows = pg_cur.fetchall()
        
        sf_cur = sf_conn.cursor()
        
        col_names_upper = [c.upper() for c in col_names]
        placeholders = ', '.join(['%s'] * len(col_names))
        insert_sql = f"INSERT INTO ODS_SWF_SA_RAW.{table_name.upper()} ({', '.join(col_names_upper)}) VALUES ({placeholders})"
        
        for row in rows:
            processed_row = []
            for val in row:
                if val is None:
                    processed_row.append(None)
                elif isinstance(val, (dict, list)):
                    processed_row.append(json.dumps(val))
                else:
                    processed_row.append(val)
            
            sf_cur.execute(insert_sql, processed_row)
        
        sf_conn.commit()
        logger.info(f"  ✓ {table_name}: {count} records")
        
        sf_cur.close()
        pg_cur.close()
        return count
        
    except Exception as e:
        logger.error(f"  ✗ {table_name}: {e}")
        return 0

def run_etl():
    logger.info("\n" + "=" * 80)
    logger.info("Fast ETL: PostgreSQL → ODS_SWF_SA_RAW (Direct SQL)")
    logger.info("=" * 80 + "\n")
    
    pg_conn = get_postgres_connection()
    sf_conn = get_snowflake_connection()
    
    clean_schema(sf_conn)
    
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
