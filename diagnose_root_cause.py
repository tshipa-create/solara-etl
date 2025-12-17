#!/usr/bin/env python3

import psycopg2
import os
import logging
from dotenv import load_dotenv

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

def diagnose_auditlog_logentry():
    logger.info("=" * 80)
    logger.info("DIAGNOSING auditlog_logentry DATA LOSS")
    logger.info("=" * 80)
    
    conn = get_postgres_connection()
    cur = conn.cursor()
    
    logger.info("\n1. SCHEMA ANALYSIS")
    logger.info("-" * 80)
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns 
        WHERE table_name = 'auditlog_logentry'
        ORDER BY ordinal_position
    """)
    logger.info("Columns in auditlog_logentry:")
    for col_name, col_type, nullable in cur.fetchall():
        null_str = "NULL" if nullable == "YES" else "NOT NULL"
        logger.info(f"  {col_name:30} | {col_type:20} | {null_str}")
    
    logger.info("\n2. DATA QUALITY CHECK")
    logger.info("-" * 80)
    cur.execute("""
        SELECT 
            COUNT(*) as total_records,
            COUNT(CASE WHEN id IS NULL THEN 1 END) as null_ids,
            COUNT(CASE WHEN timestamp IS NULL THEN 1 END) as null_timestamps,
            COUNT(CASE WHEN changes IS NULL THEN 1 END) as null_changes,
            COUNT(CASE WHEN additional_data IS NULL THEN 1 END) as null_additional_data
        FROM auditlog_logentry
    """)
    result = cur.fetchone()
    logger.info(f"Total records: {result[0]}")
    logger.info(f"NULL ids: {result[1]}")
    logger.info(f"NULL timestamps: {result[2]}")
    logger.info(f"NULL changes: {result[3]}")
    logger.info(f"NULL additional_data: {result[4]}")
    
    logger.info("\n3. CHECKING PROBLEMATIC ID RANGES")
    logger.info("-" * 80)
    logger.info("Looking at records around the gap (ID 325457-325466):")
    
    cur.execute("""
        SELECT id, timestamp, object_repr, changes IS NOT NULL, additional_data IS NOT NULL, LENGTH(CAST(changes AS text))
        FROM auditlog_logentry
        WHERE id >= 325450 AND id <= 325470
        ORDER BY id
    """)
    results = cur.fetchall()
    for row in results:
        record_id, ts, obj_repr, has_changes, has_additional, changes_len = row
        logger.info(f"  ID {record_id:7} | {str(ts)[:10]} | {obj_repr[:30]:30} | changes:{has_changes} | len:{changes_len}")
    
    if not results:
        logger.warning("  No records found in ID range 325450-325470")
        logger.info("  This suggests the gap is from newer records added AFTER ETL snapshot")
    
    logger.info("\n4. CHECKING FOR INVALID CHARACTERS / ENCODING")
    logger.info("-" * 80)
    cur.execute("""
        SELECT COUNT(*) FROM auditlog_logentry
        WHERE changes::text ~ '\\x00|\\r|\\n'
    """)
    null_bytes = cur.fetchone()[0]
    logger.info(f"Records with potentially problematic characters in changes: {null_bytes}")
    
    logger.info("\n5. CHECKING JSONB COLUMN SIZES")
    logger.info("-" * 80)
    cur.execute("""
        SELECT 
            LENGTH(CAST(changes AS text)) as changes_size,
            COUNT(*) as record_count
        FROM auditlog_logentry
        WHERE changes IS NOT NULL
        GROUP BY LENGTH(CAST(changes AS text))
        ORDER BY changes_size DESC
        LIMIT 10
    """)
    logger.info("Top 10 JSONB column sizes:")
    for size, count in cur.fetchall():
        logger.info(f"  {size:8} bytes | {count:6} records")
    
    logger.info("\n6. CONCURRENT DATA CHECK")
    logger.info("-" * 80)
    cur.execute("""
        SELECT 
            DATE(timestamp) as date,
            COUNT(*) as records
        FROM auditlog_logentry
        GROUP BY DATE(timestamp)
        ORDER BY date DESC
        LIMIT 10
    """)
    logger.info("Records by date (most recent first):")
    for date, count in cur.fetchall():
        logger.info(f"  {date} | {count:6} records")
    
    cur.close()
    conn.close()
    
    logger.info("\n" + "=" * 80)
    logger.info("NEXT STEPS:")
    logger.info("=" * 80)
    logger.info("1. If gap records exist in PG: Problem is SCHEMA/TYPE MISMATCH in dlt")
    logger.info("2. If gap records DON'T exist: Problem is CONCURRENT INSERTS during ETL")
    logger.info("=" * 80)

if __name__ == "__main__":
    diagnose_auditlog_logentry()
