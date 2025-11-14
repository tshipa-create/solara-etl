import os
import time
import logging
import logging.handlers
import boto3
import pandas as pd
import psycopg2
from io import StringIO
from snowflake.connector import connect
from snowflake.connector.pandas_tools import write_pandas
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
import certifi
from datetime import datetime

# Set SSL certificate bundle for requests/boto3
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
os.environ['AWS_CA_BUNDLE'] = certifi.where()
os.environ['AWS_SSL_VERIFY'] = 'false'  # Disable SSL verification for S3 uploads

# --------------------------------------------------
# Setup Logging
# --------------------------------------------------
# Create logs directory if it doesn't exist
os.makedirs('logs', exist_ok=True)

# Setup logging with console and daily file handler
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console_handler.setFormatter(console_formatter)

# Daily file handler
file_handler = logging.handlers.TimedRotatingFileHandler(
    'logs/'+datetime.now().strftime('%Y-%m-%d')+'.log',
    when='midnight',
    interval=1,
    backupCount=30  # Keep 30 days of logs
)
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler.setFormatter(file_formatter)

# Add handlers to logger
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# --------------------------------------------------
# Load environment variables
# --------------------------------------------------
load_dotenv()
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

# --------------------------------------------------
# AWS SSM client
# --------------------------------------------------
ssm_client = boto3.client("ssm", region_name="af-south-1")

def get_ssm_param(name):
    """Retrieve and decrypt a parameter from AWS SSM"""
    response = ssm_client.get_parameter(Name=name, WithDecryption=True)
    return response["Parameter"]["Value"]

# --------------------------------------------------
# Connect to Postgres (RDS)
# --------------------------------------------------
def get_postgres_connection():
    logger.info(f"Connecting to RDS PostgreSQL at {DB_HOST}:{DB_PORT}")
    conn = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )
    return conn

# --------------------------------------------------
# Get Snowflake connection
# --------------------------------------------------
def get_private_key():
    logger.info("Fetching Snowflake private key from SSM")
    ssm = boto3.client("ssm", region_name="us-east-1")

    key = ssm.get_parameter(
        Name="/snowflake/connection_private_key",
        WithDecryption=True
    )["Parameter"]["Value"]

    passphrase = ssm.get_parameter(
        Name="/snowflake/connection_passphrase",
        WithDecryption=True
    )["Parameter"]["Value"]

    p_key = serialization.load_pem_private_key(
        key.encode(),
        password=passphrase.encode(),
        backend=default_backend()
    )

    return p_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def get_snowflake_connection():
    logger.info("Establishing Snowflake connection")
    ssm = boto3.client("ssm", region_name="af-south-1")

    creds = {
        key.split("/")[-1]: ssm.get_parameter(Name=key, WithDecryption=True)["Parameter"]["Value"]
        for key in [
            "/odoo_etl/SNOWFLAKE_USER",
            "/odoo_etl/SNOWFLAKE_ACCOUNT",
            "/odoo_etl/SNOWFLAKE_WAREHOUSE",
            "/odoo_etl/SNOWFLAKE_DATABASE",
            "/odoo_etl/SNOWFLAKE_SCHEMA",
        ]
    }

    return connect(
        user=creds["SNOWFLAKE_USER"],
        account=creds["SNOWFLAKE_ACCOUNT"],
        private_key=get_private_key(),
        warehouse=creds["SNOWFLAKE_WAREHOUSE"],
        database=creds["SNOWFLAKE_DATABASE"],
        schema='ODS_SOLARA',
        insecure_mode=True,  # Bypass SSL certificate validation
    )

# --------------------------------------------------
# Get list of tables from Postgres
# --------------------------------------------------
def get_all_tables(pg_conn):
    query = """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_name;
    """
    df = pd.read_sql(query, pg_conn)
    return df["table_name"].tolist()

# --------------------------------------------------
# Extract data from Postgres
# --------------------------------------------------
def extract_table(pg_conn, table_name):
    query = f'SELECT * FROM public."{table_name}"'
    df = pd.read_sql(query, pg_conn)

    df["load_at_ts_utc"] = pd.Timestamp.utcnow()

    # Handle JSON columns specifically to prevent type inference issues
    for col in df.columns:
        if df[col].dtype == 'object':  # Potential JSON columns
            # Convert to string but handle None values
            df[col] = df[col].astype(str)
            df[col] = df[col].replace({'None': None, 'nan': None, 'NaT': None})

    return df

# --------------------------------------------------
# Load data to Snowflake
# --------------------------------------------------
def load_to_snowflake(sf_conn, df, table_name):
    try:
        # Drop table if exists to ensure clean schema recreation
        cursor = sf_conn.cursor()
        cursor.execute(f"DROP TABLE IF EXISTS {table_name.upper()}")
        cursor.close()

        # Load data using write_pandas with auto_create_table
        success, nchunks, nrows, _ = write_pandas(
            conn=sf_conn,
            df=df,
            table_name=table_name.upper(),
            auto_create_table=True,
            overwrite=True
        )
        return success, nrows
    except Exception as e:
        logger.error(f"Error loading {table_name}: {e}")
        return False, 0

# --------------------------------------------------
# Main ETL Logic
# --------------------------------------------------
def main():
    logger.info("Establishing Snowflake connection")
    sf_conn = get_snowflake_connection()

    logger.info("Establishing PostgreSQL connection")
    pg_conn = get_postgres_connection()

    tables = get_all_tables(pg_conn)
    logger.info(f"Found {len(tables)} tables in schema 'public'")
    logger.info("Starting data extraction and loading")

    for i, table in enumerate(tables, 1):
        try:
            logger.info(f"[{i}/{len(tables)}] Processing {table}...")
            df = extract_table(pg_conn, table)
            if df.empty:
                logger.info(f"{table}: No data found, skipping.")
                continue

            success, nrows = load_to_snowflake(sf_conn, df, table)
            if success:
                logger.info(f"{table}: Loaded {nrows} rows successfully.")
            else:
                logger.error(f"{table}: Load failed.")

        except Exception as e:
            logger.error(f"{table}: Error - {e}")
        finally:
            time.sleep(1)

    pg_conn.close()
    sf_conn.close()
    logger.info("Data load complete.")

# --------------------------------------------------
# Run Script
# --------------------------------------------------
if __name__ == "__main__":
    main()
