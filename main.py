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

# --------------------------------------------------
# Setup Logging
# --------------------------------------------------
os.makedirs('logs', exist_ok=True)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console_handler.setFormatter(console_formatter)

file_handler = logging.handlers.TimedRotatingFileHandler(
    'logs/solar_to_snowflake.log',
    when='midnight',
    interval=1,
    backupCount=30
)
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler.setFormatter(file_formatter)

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

    try:
        return connect(
            user=creds["SNOWFLAKE_USER"],
            account=creds["SNOWFLAKE_ACCOUNT"],
            private_key=get_private_key(),
            warehouse=creds["SNOWFLAKE_WAREHOUSE"],
            database=creds["SNOWFLAKE_DATABASE"],
            schema='ODS_SOLARA',
            insecure_mode=True,
            session_parameters={
                'CLIENT_SESSION_KEEP_ALIVE': True,
                'CLIENT_SESSION_KEEP_ALIVE_HEARTBEAT_FREQUENCY': 3600,
                'TIMEZONE': 'UTC',
            },
        )
    except Exception as e:
        logger.warning(f"Initial connection failed: {e}. Trying with additional SSL bypass...")
        return connect(
            user=creds["SNOWFLAKE_USER"],
            account=creds["SNOWFLAKE_ACCOUNT"],
            private_key=get_private_key(),
            warehouse=creds["SNOWFLAKE_WAREHOUSE"],
            database=creds["SNOWFLAKE_DATABASE"],
            schema='ODS_SOLARA',
            insecure_mode=True,
            session_parameters={
                'CLIENT_SESSION_KEEP_ALIVE': True,
                'CLIENT_SESSION_KEEP_ALIVE_HEARTBEAT_FREQUENCY': 3600,
                'TIMEZONE': 'UTC',
            },
            ssl_disabled=True,
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
# Extract data from Postgres (fixed cleanup)
# --------------------------------------------------
def extract_table(pg_conn, table_name):
    query = f'SELECT * FROM public."{table_name}"'
    df = pd.read_sql(query, pg_conn)

    # Columns that should be numeric
    numeric_cols = {
        'p42_applicationlead': ['raw_lead_data'],
        'p42_entrancescoring': ['primary_results', 'response_data']
    }
    if table_name.lower() in numeric_cols:
        for col in numeric_cols[table_name.lower()]:
            if col in df.columns:
                df[col] = df[col].replace('', None)
                df[col] = pd.to_numeric(df[col], errors='coerce')

    # Drop problematic struct columns
    struct_columns_to_drop = {
        'p42_applications': ['step_data'],
        'p42_contract': ['application_state'],
        'p42_creditcheck': ['bond_details'],
        'p42_outgoingmessage': ['content_variables']
    }
    if table_name.lower() in struct_columns_to_drop:
        for col in struct_columns_to_drop[table_name.lower()]:
            if col in df.columns:
                logger.info(f"Dropping problematic struct column '{col}' from {table_name}")
                df = df.drop(columns=[col])
            elif col.lower() in [c.lower() for c in df.columns]:
                actual_col = next(c for c in df.columns if c.lower() == col.lower())
                logger.info(f"Dropping problematic struct column '{actual_col}' from {table_name} (case-insensitive match)")
                df = df.drop(columns=[actual_col])

    # Convert object columns safely
    for col in df.select_dtypes(include=['object']).columns:
        df[col] = df[col].replace(['', 'nan', 'NaN', 'NULL', 'null'], None)
        df[col] = df[col].apply(lambda x: str(x) if x is not None else None)

    df["load_at_ts_utc"] = pd.Timestamp.utcnow()
    return df

# --------------------------------------------------
# Load data to Snowflake
# --------------------------------------------------
def load_to_snowflake(sf_conn, df, table_name):
    try:
        df.columns = df.columns.str.upper()
        logger.info(f"Loading {len(df)} rows to {table_name.upper()}")

        success, nchunks, nrows, _ = write_pandas(
            conn=sf_conn,
            df=df,
            table_name=table_name.upper(),
            auto_create_table=True,
            overwrite=True
        )

        if success:
            logger.info(f"Successfully loaded {nrows} rows in {nchunks} chunks")
        else:
            logger.error(f"Failed to load data to {table_name.upper()}")

        return success, nrows

    except Exception as e:
        logger.error(f"Error loading to {table_name.upper()}: {str(e)}")
        # Retry with smaller chunks
        try:
            logger.info(f"Retrying {table_name.upper()} with smaller chunks")
            success, nchunks, nrows, _ = write_pandas(
                conn=sf_conn,
                df=df,
                table_name=table_name.upper(),
                auto_create_table=True,
                overwrite=True,
                chunk_size=50000
            )
            return success, nrows
        except Exception as retry_e:
            logger.error(f"Retry also failed for {table_name.upper()}: {str(retry_e)}")
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

    failed_tables = []
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
                failed_tables.append(table)

        except Exception as e:
            logger.error(f"{table}: Error - {e}")
            failed_tables.append(table)
        finally:
            time.sleep(1)

    if failed_tables:
        logger.warning(f"The following tables failed to load: {', '.join(failed_tables)}")
        logger.info(f"Total failed tables: {len(failed_tables)} out of {len(tables)}")

    pg_conn.close()
    sf_conn.close()
    logger.info("Data load complete.")

# --------------------------------------------------
# Run Script
# --------------------------------------------------
if __name__ == "__main__":
    main()
