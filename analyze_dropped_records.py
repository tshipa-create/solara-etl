#!/usr/bin/env python3
import psycopg2
import json
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

def analyze_table(table_name, id_range_start, id_range_end):
    logger.info(f"\n{'=' * 80}")
    logger.info(f"Analyzing dropped records in {table_name}")
    logger.info(f"ID Range: {id_range_start}-{id_range_end}")
    logger.info(f"{'=' * 80}\n")
    
    conn = get_postgres_connection()
    cur = conn.cursor()
    
    cur.execute(f"""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
    """, (table_name,))
    
    columns = cur.fetchall()
    col_names = [col[0] for col in columns]
    logger.info(f"Table has {len(col_names)} columns:\n")
    for col_name, col_type in columns[:10]:
        logger.info(f"  {col_name}: {col_type}")
    if len(columns) > 10:
        logger.info(f"  ... and {len(columns) - 10} more")
    
    cur.execute(f"""
        SELECT id
        FROM public."{table_name}"
        WHERE id BETWEEN %s AND %s
        LIMIT 1
    """, (id_range_start, id_range_end))
    
    row = cur.fetchone()
    if row:
        logger.info(f"\nSample record exists: ID {row[0]}")
    
    cur.execute(f"""
        SELECT 
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE id BETWEEN %s AND %s) as in_range
        FROM public."{table_name}"
    """, (id_range_start, id_range_end))
    
    stats = cur.fetchone()
    logger.info(f"\nRecord Statistics:")
    logger.info(f"  Total records: {stats[0]}")
    logger.info(f"  In dropped range: {stats[1]}")
    
    cur.execute(f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = %s
        AND data_type IN ('jsonb', 'json')
    """, (table_name,))
    
    json_cols = [row[0] for row in cur.fetchall()]
    if json_cols:
        logger.info(f"\nJSON/JSONB columns: {', '.join(json_cols)}")
        
        for json_col in json_cols:
            cur.execute(f"""
                SELECT 
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE {json_col} IS NULL) as nulls
                FROM public."{table_name}"
                WHERE id BETWEEN %s AND %s
            """, (id_range_start, id_range_end))
            
            result = cur.fetchone()
            if result:
                logger.info(f"\n  {json_col}:")
                logger.info(f"    Total: {result[0]}, NULLs: {result[1]}")
    
    cur.execute(f"""
        SELECT id
        FROM public."{table_name}"
        WHERE id BETWEEN %s AND %s
        ORDER BY id
        LIMIT 5
    """, (id_range_start, id_range_end))
    
    rows = cur.fetchall()
    logger.info(f"\nFirst 5 dropped records:")
    for idx, row_data in enumerate(rows, 1):
        logger.info(f"  Record {idx}: ID={row_data[0]}")
    
    cur.close()
    conn.close()

def main():
    logger.info("\n" + "=" * 80)
    logger.info("DETAILED ANALYSIS OF DROPPED RECORDS")
    logger.info("=" * 80)
    
    analyze_table('auditlog_logentry', 342679, 342766)
    analyze_table('p42_vehicledata', 59656, 59680)
    
    logger.info("\n" + "=" * 80)
    logger.info("NEXT STEPS")
    logger.info("=" * 80)
    logger.info("\n1. Check if data has issues (NULL JSON, invalid types, etc)")
    logger.info("2. Look at dlt pipeline logs for specific error messages")
    logger.info("3. Test serialization with actual data from PostgreSQL")
    logger.info("=" * 80 + "\n")

if __name__ == "__main__":
    main()
