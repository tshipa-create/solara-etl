import os
import sys
import json
import datetime
import logging
import hashlib
import time
from typing import List, Tuple, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import request

import psycopg2
import snowflake.connector
import boto3
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
import certifi

os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
os.environ['AWS_CA_BUNDLE'] = certifi.where()
os.environ['AWS_SSL_VERIFY'] = 'false'

try:
    import watchtower
    WATCHTOWER_AVAILABLE = True
except Exception:
    WATCHTOWER_AVAILABLE = False

load_dotenv()

TARGET_SCHEMA = 'ODS_SOLARA'

def setup_logging() -> logging.Logger:
    logger = logging.getLogger("etl_pipeline")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    os.makedirs("logs", exist_ok=True)
    date_stamp = datetime.datetime.now().strftime("%Y%m%d")
    file_path = f"logs/etl_solara_snow_{date_stamp}.log"
    fh = logging.FileHandler(file_path)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    if WATCHTOWER_AVAILABLE:
        try:
            logs_group = os.getenv("CLOUDWATCH_LOG_GROUP", "/aws/ssm/solara-etl")
            logs_stream = os.getenv("CLOUDWATCH_LOG_STREAM", "production-run")
            region = os.getenv("AWS_REGION", "af-south-1")
            cw = watchtower.CloudWatchLogHandler(
                log_group=logs_group,
                stream_name=logs_stream,
                boto3_client=boto3.client("logs", region_name=region),
            )
            cw.setFormatter(formatter)
            logger.addHandler(cw)
        except Exception:
            logger.error("CloudWatch handler setup failed", exc_info=True)
    else:
        logger.warning("watchtower not installed, CloudWatch logging disabled")

    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)

    return logger


logger = setup_logging()


def get_latest_log_stream(log_group: str, region: str) -> Optional[str]:
    try:
        logs_client = boto3.client("logs", region_name=region)
        response = logs_client.describe_log_streams(
            logGroupName=log_group,
            orderBy="LastEventTime",
            descending=True,
            limit=1
        )
        if response.get("logStreams"):
            return response["logStreams"][0]["logStreamName"]
    except Exception as e:
        logger.warning(f"Failed to fetch log stream: {e}")
    return None


def send_slack_summary(summary_data: Dict[str, Any], results: List[Dict[str, Any]] = None):
    bot_token = os.getenv("SLACK_BOT_TOKEN")
    channel_id = os.getenv("SLACK_CHANNEL_ID")
    
    if not bot_token or not channel_id:
        logger.warning("SLACK_BOT_TOKEN or SLACK_CHANNEL_ID not set. Skipping notification.")
        return

    if results is None:
        results = []

    duration = summary_data.get("duration", 0)
    minutes, seconds = divmod(int(duration), 60)
    duration_str = f"{minutes}m {seconds}s"

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    failures = summary_data.get("failures", 0)

    failed_results = [r for r in results if r["status"] != "SUCCESS"]
    success_results = [r for r in results if r["status"] == "SUCCESS"]

    failures_section = ""
    if failed_results:
        failures_section = "*🚨 Failures (" + str(len(failed_results)) + ")*\n"
        for result in failed_results:
            table = result["table"]
            error = result.get("error", "Unknown error")
            pg_count = result.get("pg_count", "?")
            sf_count = result.get("sf_count", "?")
            
            if pg_count != "?" and sf_count != "?":
                failures_section += f"`{table}` - {error} (PG: {pg_count:,} → SNOW: {sf_count:,})\n"
            else:
                failures_section += f"`{table}` - {error}\n"

    successes_section = ""
    if success_results:
        success_names = " ".join([f"`{r['table']}`" for r in success_results])
        successes_section = f"*✅ Successes ({len(success_results)})*\n{success_names}\n"

    status_text = "Sync Failed" if failures > 0 else "Sync Completed"
    title = f"Solara ETL: {status_text}"

    message_text = f"{title} | {timestamp} | {duration_str}\n\n"
    if failures_section:
        message_text += failures_section + "\n"
    if successes_section:
        message_text += successes_section

    log_group = os.getenv("CLOUDWATCH_LOG_GROUP", "/aws/ssm/solara-etl")
    region = os.getenv("AWS_REGION", "af-south-1")
    
    log_stream = get_latest_log_stream(log_group, region)
    if not log_stream:
        log_stream = os.getenv("CLOUDWATCH_LOG_STREAM", "production-run")
    
    cloudwatch_url = f"https://console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/{log_group}/log-events/{log_stream}"

    payload = {
        "channel": channel_id,
        "text": message_text,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": message_text
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"📋 <{cloudwatch_url}|View CloudWatch Logs>"
                    }
                ]
            }
        ]
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json"
        }
        req = request.Request(
            "https://slack.com/api/chat.postMessage",
            data=data,
            headers=headers,
            method="POST"
        )
        with request.urlopen(req) as response:
            result = json.loads(response.read().decode())
            if result.get("ok"):
                logger.info("Successfully sent Slack notification.")
            else:
                logger.warning(f"Failed to send Slack notification: {result.get('error')}")
    except Exception:
        logger.error("Error sending Slack notification", exc_info=True)


def quote_identifier(identifier: str, uppercase: bool = False) -> str:
    if uppercase:
        return f'"{identifier.upper()}"'
    return f'"{identifier}"'


def retry_with_backoff(func, max_retries: int = 3, base_delay: float = 1.0):
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(f"Attempt {attempt + 1} failed, retrying in {delay}s: {e}")
            time.sleep(delay)


def calculate_row_hash(row: Tuple) -> str:
    row_str = json.dumps(row, default=str, sort_keys=True)
    return hashlib.md5(row_str.encode()).hexdigest()


def log_endpoints():
    logger.info(f"Postgres: host={os.getenv('DB_HOST')} port={os.getenv('DB_PORT', '5432')} db={os.getenv('DB_NAME')} user={os.getenv('DB_USER')}")
    logger.info("Snowflake: using SSM (us-east-1 for key, af-south-1 for account/user/db/wh)")


def get_postgres_conn():
    missing = [k for k in ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"] if not os.getenv(k)]
    if missing:
        logger.critical(f"Missing Postgres env vars: {missing}")
        raise RuntimeError("Postgres configuration incomplete")

    conn = psycopg2.connect(
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT", "5432"),
        sslmode=os.getenv("DB_SSLMODE", "require"),
        connect_timeout=10,
    )
    return conn


def get_snowflake_config_from_ssm():
    try:
        ssm_us = boto3.client("ssm", region_name="us-east-1")
        key_pem = ssm_us.get_parameter(Name="/snowflake/connection_private_key", WithDecryption=True)["Parameter"]["Value"]
        passphrase = ssm_us.get_parameter(Name="/snowflake/connection_passphrase", WithDecryption=True)["Parameter"]["Value"]

        p_key = serialization.load_pem_private_key(key_pem.encode(), password=passphrase.encode(), backend=default_backend())
        private_key_bytes = p_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        ssm_af = boto3.client("ssm", region_name="af-south-1")
        def fetch(key): return ssm_af.get_parameter(Name=key, WithDecryption=True)["Parameter"]["Value"]

        return {
            "host": fetch("/odoo_etl/SNOWFLAKE_ACCOUNT"),
            "username": fetch("/odoo_etl/SNOWFLAKE_USER"),
            "database": fetch("/odoo_etl/SNOWFLAKE_DATABASE"),
            "warehouse": fetch("/odoo_etl/SNOWFLAKE_WAREHOUSE"),
            "role": "SYSADMIN", 
            "private_key": private_key_bytes,
        }
    except Exception as e:
        logger.error("SSM Load Failed", exc_info=True)
        raise e

def get_snowflake_conn():
    cfg = get_snowflake_config_from_ssm()
    return snowflake.connector.connect(
        account=cfg["host"],
        user=cfg["username"],
        private_key=cfg["private_key"],
        warehouse=cfg["warehouse"],
        database=cfg["database"],
        role=cfg["role"],
    )


def test_connections():
    logger.info("Testing connections...")
    try:
        with get_postgres_conn() as pg_conn:
            with pg_conn.cursor() as c:
                c.execute("SELECT 1")
        logger.info("[OK] Postgres connection and basic query succeeded")
    except Exception:
        logger.critical("[FAIL] Postgres connection test failed", exc_info=True)
        raise

    try:
        with get_snowflake_conn() as sf_conn:
            with sf_conn.cursor() as c:
                c.execute("SELECT 1")
        logger.info("[OK] Snowflake connection and basic query succeeded")
    except Exception:
        logger.critical("[FAIL] Snowflake connection test failed", exc_info=True)
        raise


PG_TO_SNOWFLAKE = {
    "integer": "INTEGER",
    "bigint": "BIGINT",
    "smallint": "SMALLINT",
    "numeric": "NUMBER",
    "double precision": "FLOAT",
    "real": "FLOAT",
    "boolean": "BOOLEAN",
    "text": "TEXT",
    "character varying": "TEXT",
    "character": "TEXT",
    "uuid": "TEXT",
    "bytea": "BINARY",
    "timestamp without time zone": "TIMESTAMP_NTZ",
    "timestamp with time zone": "TIMESTAMP_TZ",
    "date": "DATE",
    "time without time zone": "TIME",
    "time with time zone": "TIME",
    "json": "VARIANT",
    "jsonb": "VARIANT",
    "inet": "TEXT",
}
JSON_TYPES = {"json", "jsonb"}

def map_pg_type(pg_type: str) -> str:
    return PG_TO_SNOWFLAKE.get(pg_type, "TEXT")


def fetch_columns(pg_cur, schema: str, table: str) -> List[Tuple[str, str]]:
    pg_cur.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    return pg_cur.fetchall()


def build_create_table_sql(target_schema: str, table: str, cols: List[Tuple[str, str]]) -> str:
    col_defs = [f'{quote_identifier(col, uppercase=True)} {map_pg_type(pg_type)}' for col, pg_type in cols]
    col_defs.append(f'{quote_identifier("LOADED_AT_UTC", uppercase=True)} TIMESTAMP_TZ')
    return f'CREATE OR REPLACE TABLE {quote_identifier(target_schema, uppercase=True)}.{quote_identifier(table, uppercase=True)} ({", ".join(col_defs)})'


def build_insert_sql(target_schema: str, table: str, cols: List[Tuple[str, str]]) -> str:
    col_list = ", ".join([quote_identifier(c[0], uppercase=True) for c in cols])
    select_list = []
    for _, pg_type in cols:
        if pg_type in JSON_TYPES:
            select_list.append('PARSE_JSON(%s)')
        else:
            select_list.append('%s')
    select_clause = ", ".join(select_list)
    return f'INSERT INTO {quote_identifier(target_schema, uppercase=True)}.{quote_identifier(table, uppercase=True)} ({col_list}) SELECT {select_clause}'


def format_row_for_insert(cols: List[Tuple[str, str]], row: Tuple) -> List:
    formatted = []
    for (_, pg_type), value in zip(cols, row):
        if pg_type in JSON_TYPES:
            if value is None:
                formatted.append(None)
            else:
                if isinstance(value, (dict, list)):
                    formatted.append(json.dumps(value))
                else:
                    formatted.append(str(value))
        elif pg_type == "bytea":
            if value is None:
                formatted.append(None)
            else:
                try:
                    if isinstance(value, memoryview):
                        formatted.append(bytes(value).hex())
                    elif isinstance(value, bytes):
                        formatted.append(value.hex())
                    else:
                        formatted.append(value)
                except Exception as e:
                    logger.warning(f"Failed to convert bytea: {type(value)} - {e}")
                    formatted.append(None)
        else:
            formatted.append(value)
    return formatted


def validate_counts(pg_cur, sf_cur, pg_schema: str, pg_table: str, target_schema: str, sf_table: str = None) -> Tuple[bool, int, int]:
    if sf_table is None:
        sf_table = pg_table
    
    pg_cur.execute(f'SELECT COUNT(*) FROM {quote_identifier(pg_schema)}.{quote_identifier(pg_table)}')
    pg_count = pg_cur.fetchone()[0]

    sf_cur.execute(f'SELECT COUNT(*) FROM {quote_identifier(target_schema, uppercase=True)}.{quote_identifier(sf_table, uppercase=True)}')
    sf_count = sf_cur.fetchone()[0]

    if pg_count == sf_count:
        logger.info(f"[OK] COUNT MATCH: {pg_table} (PG: {pg_count} == SNOW: {sf_count})")
        return True, pg_count, sf_count
    else:
        logger.error(f"[MISMATCH] COUNT ERROR: {pg_table} (PG: {pg_count} != SNOW: {sf_count})")
        return False, pg_count, sf_count


def validate_json_variant(sf_cur, target_schema: str, table: str, pg_cols: List[Tuple[str, str]]) -> bool:
    pg_json_cols = [col for col, pg_type in pg_cols if pg_type in JSON_TYPES]
    if not pg_json_cols:
        return True

    placeholders = ",".join(["%s"] * len(pg_json_cols))
    sf_cur.execute(
        f"""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND column_name IN ({placeholders})
        """,
        (target_schema.upper(), table.upper(), *[c.upper() for c in pg_json_cols]),
    )
    result = {r[0]: r[1] for r in sf_cur.fetchall()}

    ok = True
    for col in pg_json_cols:
        sf_type = result.get(col.upper(), "MISSING")
        if sf_type == "VARIANT":
            logger.info(f"[OK] TYPE MATCH: {table}.{col} -> VARIANT")
        else:
            logger.error(f"[ERROR] TYPE MISMATCH: {table}.{col} is {sf_type} (Expected VARIANT)")
            ok = False
    return ok


def recreate_final_and_staging(sf_cur, target_schema: str, table: str, cols: List[Tuple[str, str]]) -> str:
    final_sql = build_create_table_sql(target_schema, table, cols)
    logger.info(f"Ensuring final table exists: {final_sql}")
    retry_with_backoff(lambda: sf_cur.execute(final_sql))

    staging_table = f"{table}__STAGING"
    sf_cur.execute(f'DROP TABLE IF EXISTS {quote_identifier(target_schema, uppercase=True)}.{quote_identifier(staging_table, uppercase=True)}')
    staging_sql = final_sql.replace(quote_identifier(table, uppercase=True), quote_identifier(staging_table, uppercase=True))
    logger.info(f"Creating staging table: {staging_sql}")
    sf_cur.execute(staging_sql)

    return staging_table


def load_into_staging(pg_cur, sf_cur, pg_schema: str, source_table: str, target_schema: str, staging_table: str, cols: List[Tuple[str, str]], batch_size: int):
    pg_cur.execute(f'SELECT * FROM {quote_identifier(pg_schema)}.{quote_identifier(source_table)}')
    
    total_rows = 0
    loaded_at_utc = datetime.datetime.now(datetime.timezone.utc)
    while True:
        rows = pg_cur.fetchmany(batch_size)
        if not rows:
            break

        formatted_batch = [tuple(format_row_for_insert(cols, r)) for r in rows]
        
        col_list = ", ".join([quote_identifier(c[0], uppercase=True) for c in cols])
        col_list += f", {quote_identifier('LOADED_AT_UTC', uppercase=True)}"
        col_defs = []
        for _, pg_type in cols:
            if pg_type in JSON_TYPES:
                col_defs.append('PARSE_JSON(%s)')
            elif pg_type == "bytea":
                col_defs.append('HEX_DECODE_BINARY(%s)')
            else:
                col_defs.append('%s')
        col_defs.append('%s')
        
        union_selects = []
        all_params = []
        for formatted_row in formatted_batch:
            union_selects.append(f"SELECT {', '.join(col_defs)}")
            all_params.extend(formatted_row)
            all_params.append(loaded_at_utc)
        
        batch_insert_sql = f'INSERT INTO {quote_identifier(target_schema, uppercase=True)}.{quote_identifier(staging_table, uppercase=True)} ({col_list}) {" UNION ALL ".join(union_selects)}'
        
        retry_with_backoff(lambda sql=batch_insert_sql, params=all_params: sf_cur.execute(sql, params))
        total_rows += len(formatted_batch)
        logger.info(f"Inserted {total_rows} rows into staging {staging_table}")


def swap_staging_to_final(sf_cur, target_schema: str, table: str, staging_table: str, keep_backup: bool = False):
    final_full = f'{quote_identifier(target_schema, uppercase=True)}.{quote_identifier(table, uppercase=True)}'
    staging_full = f'{quote_identifier(target_schema, uppercase=True)}.{quote_identifier(staging_table, uppercase=True)}'
    backup_name = f"{table}__BACKUP_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup_full = f'{quote_identifier(target_schema, uppercase=True)}.{quote_identifier(backup_name, uppercase=True)}'

    logger.info(f"Swapping staging into final for {table}")
    sf_cur.execute(f'ALTER TABLE IF EXISTS {final_full} RENAME TO {backup_full}')
    sf_cur.execute(f'ALTER TABLE {staging_full} RENAME TO {final_full}')
    if not keep_backup:
        sf_cur.execute(f'DROP TABLE IF EXISTS {backup_full}')


def setup_metadata_table(sf_cur, target_schema: str) -> None:
    metadata_table = f'{quote_identifier(target_schema, uppercase=True)}.{quote_identifier("ETL_METADATA", uppercase=True)}'
    sf_cur.execute(f'''
        CREATE TABLE IF NOT EXISTS {metadata_table} (
            TABLE_NAME TEXT,
            SOURCE_SCHEMA TEXT,
            LOAD_TYPE TEXT,
            ROWS_LOADED BIGINT,
            ROWS_VALIDATED BIGINT,
            LOAD_START TIMESTAMP_TZ,
            LOAD_END TIMESTAMP_TZ,
            LOADED_AT_UTC TIMESTAMP_TZ,
            LAST_SOURCE_ID BIGINT,
            ROW_HASH TEXT,
            STATUS TEXT,
            ERROR_MESSAGE TEXT,
            PRIMARY KEY (TABLE_NAME, LOAD_START)
        )
    ''')
    logger.info(f"Metadata table ready: {metadata_table}")


def record_load_metadata(sf_cur, target_schema: str, table_name: str, source_schema: str, 
                        load_type: str, rows_loaded: int, rows_validated: int, 
                        load_start: datetime.datetime, status: str = "SUCCESS", error_msg: str = None):
    metadata_table = f'{quote_identifier(target_schema, uppercase=True)}.{quote_identifier("ETL_METADATA", uppercase=True)}'
    load_end = datetime.datetime.now(datetime.timezone.utc)
    loaded_at_utc = datetime.datetime.now(datetime.timezone.utc)
    sf_cur.execute(f'''
        INSERT INTO {metadata_table} 
        (TABLE_NAME, SOURCE_SCHEMA, LOAD_TYPE, ROWS_LOADED, ROWS_VALIDATED, LOAD_START, LOAD_END, LOADED_AT_UTC, STATUS, ERROR_MESSAGE)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ''', (table_name, source_schema, load_type, rows_loaded, rows_validated, load_start, load_end, loaded_at_utc, status, error_msg))


def load_table(pg_conn, sf_conn, table_name: str, pg_schema: str = "public", target_schema: str = None, batch_size: int = 5000, incremental: bool = False) -> Dict[str, Any]:
    if target_schema is None:
        target_schema = TARGET_SCHEMA
    
    load_start = datetime.datetime.now(datetime.timezone.utc)
    logger.info(f"--- Processing table: {table_name} (incremental={incremental}) ---")
    
    result = {
        "table": table_name,
        "status": "FAILED",
        "rows_loaded": 0,
        "rows_validated": 0,
        "pg_count": 0,
        "sf_count": 0,
        "error": None
    }

    pg_cur = pg_conn.cursor()
    sf_cur = sf_conn.cursor()

    try:
        cols = fetch_columns(pg_cur, pg_schema, table_name)
        staging_table = recreate_final_and_staging(sf_cur, target_schema, table_name, cols)

        load_into_staging(pg_cur, sf_cur, pg_schema, table_name, target_schema, staging_table, cols, batch_size)
        sf_conn.commit()

        pg_cur.execute(f'SELECT COUNT(*) FROM {quote_identifier(pg_schema)}.{quote_identifier(table_name)}')
        rows_loaded = pg_cur.fetchone()[0]
        result["rows_loaded"] = rows_loaded

        counts_ok, pg_count, sf_count = validate_counts(pg_cur, sf_cur, pg_schema, table_name, target_schema, staging_table)
        result["pg_count"] = pg_count
        result["sf_count"] = sf_count
        types_ok = validate_json_variant(sf_cur, target_schema, staging_table, cols)

        if counts_ok and types_ok:
            swap_staging_to_final(sf_cur, target_schema, table_name, staging_table, keep_backup=False)
            sf_conn.commit()
            logger.info(f"[OK] Swapped {staging_table} into final {table_name}")
            result["status"] = "SUCCESS"
            result["rows_validated"] = rows_loaded
        else:
            logger.error(f"[FAIL] Validation failed for {table_name}. Keeping previous final and dropping staging.")
            sf_cur.execute(f'DROP TABLE IF EXISTS {quote_identifier(target_schema, uppercase=True)}.{quote_identifier(staging_table, uppercase=True)}')
            sf_conn.commit()
            result["status"] = "VALIDATION_FAILED"
            result["error"] = "Count or type validation failed"

    except Exception as e:
        logger.error(f"Exception loading table {table_name}: {e}", exc_info=True)
        result["status"] = "ERROR"
        result["error"] = str(e)
        pg_conn.rollback()
        sf_conn.rollback()
    finally:
        try:
            record_load_metadata(sf_cur, target_schema, table_name, pg_schema, "FULL_LOAD", 
                               result["rows_loaded"], result["rows_validated"], load_start, 
                               result["status"], result["error"])
            sf_conn.commit()
        except Exception as e:
            logger.warning(f"Failed to record metadata for {table_name}: {e}")
        
        pg_cur.close()
        sf_cur.close()
        logger.info(f"--- Finished table: {table_name} | Status: {result['status']} ---")
    
    return result


def run(num_workers: int = 1, batch_size: int = 5000):
    start_time = time.time()
    summary_data = {
        "status": "FAILURE",
        "tables_processed": 0,
        "successes": 0,
        "failures": 0,
        "total_rows": 0,
        "failed_tables": [],
    }

    pg_conn = None
    sf_conn = None

    try:
        logger.info("--- STARTING ETL PIPELINE (Postgres -> Snowflake) ---")
        log_endpoints()
        test_connections()

        pg_conn = get_postgres_conn()
        sf_conn = get_snowflake_conn()

        setup_metadata_table(sf_conn.cursor(), TARGET_SCHEMA)
        sf_conn.commit()

        excluded_tables = {"auditlog_logentry", "p42_vehicleauditlog"}
        
        with pg_conn.cursor() as c:
            c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name")
            tables = [r[0] for r in c.fetchall() if r[0] not in excluded_tables]

        logger.info(f"Found {len(tables)} tables to process. Starting parallel load with {num_workers} workers")
        
        results = []
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {executor.submit(load_table, get_postgres_conn(), get_snowflake_conn(), table, "public", TARGET_SCHEMA, batch_size): table for table in tables}

            for future in as_completed(futures):
                table = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    logger.error(f"Worker failed for table {table}: {e}", exc_info=True)
                    results.append({"table": table, "status": "WORKER_ERROR", "rows_loaded": 0, "rows_validated": 0, "error": str(e)})

        summary_data["tables_processed"] = len(tables)
        summary_data["successes"] = sum(1 for r in results if r["status"] == "SUCCESS")
        summary_data["failures"] = sum(1 for r in results if r["status"] != "SUCCESS")
        summary_data["total_rows"] = sum(r["rows_loaded"] for r in results)
        summary_data["failed_tables"] = [(r["table"], r.get("error", "Unknown")) for r in results if r["status"] != "SUCCESS"]

        if summary_data["failures"] == 0 and summary_data["tables_processed"] > 0:
            summary_data["status"] = "SUCCESS"

        logger.info(f"ETL complete. Tables processed: {summary_data['tables_processed']} | SUCCESS: {summary_data['successes']} | FAILED: {summary_data['failures']}")
        logger.info(f"Total rows loaded: {summary_data['total_rows']}")
        
        for table, error in summary_data["failed_tables"]:
            logger.warning(f"  {table}: FAILED - {error}")

    except Exception as e:
        logger.critical(f"ETL pipeline failed: {e}", exc_info=True)
        summary_data["status"] = "CRITICAL_FAILURE"
        summary_data["failed_tables"].append(("Pipeline Level", str(e)))

    finally:
        if pg_conn:
            try:
                pg_conn.close()
            except Exception: pass
        if sf_conn:
            try:
                sf_conn.close()
            except Exception: pass
        
        end_time = time.time()
        summary_data["duration"] = end_time - start_time
        send_slack_summary(summary_data, results if 'results' in locals() else [])


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run the Solara ETL pipeline.")
    parser.add_argument("--workers", type=int, default=4, help="Number of parallel workers for table loading.")
    parser.add_argument("--batch_size", type=int, default=5000, help="Number of rows to fetch and insert in a single batch.")
    args = parser.parse_args()

    run(num_workers=args.workers, batch_size=args.batch_size)
