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

def serialize_json_for_variant(row):
    """Test the serialization function"""
    if not isinstance(row, dict):
        return row
    
    for col_name, value in list(row.items()):
        if value is None:
            continue
        
        col_type = type(value).__name__
        
        if col_type in ['dict', 'list']:
            row[col_name] = json.dumps(value)
    
    return row

def test_serialization():
    logger.info("=" * 80)
    logger.info("Testing serialization on actual dropped records")
    logger.info("=" * 80 + "\n")
    
    conn = get_postgres_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT json_build_object(
            'id', id,
            'object_pk', object_pk,
            'changes', changes,
            'additional_data', additional_data,
            'serialized_data', serialized_data
        ) as row_json
        FROM public."auditlog_logentry"
        WHERE id BETWEEN 342679 AND 342683
    """)
    
    rows = cur.fetchall()
    logger.info(f"Testing with {len(rows)} records from auditlog_logentry\n")
    
    for idx, (row_json,) in enumerate(rows, 1):
        try:
            row_dict = row_json
            logger.info(f"Record {idx} (ID {row_dict['id']}):")
            logger.info(f"  Before: changes type = {type(row_dict['changes']).__name__}")
            
            row_dict = serialize_json_for_variant(row_dict)
            
            logger.info(f"  After:  changes type = {type(row_dict['changes']).__name__}")
            if isinstance(row_dict['changes'], str):
                logger.info(f"  ✓ Successfully serialized to JSON string")
            logger.info()
            
        except Exception as e:
            logger.error(f"Record {idx} FAILED: {e}\n")
    
    logger.info("\nTesting p42_vehicledata:")
    cur.execute("""
        SELECT json_build_object(
            'id', id,
            'data', data,
            'vehicle_id', vehicle_id
        ) as row_json
        FROM public."p42_vehicledata"
        WHERE id BETWEEN 59656 AND 59660
    """)
    
    rows = cur.fetchall()
    logger.info(f"Testing with {len(rows)} records from p42_vehicledata\n")
    
    for idx, (row_json,) in enumerate(rows, 1):
        try:
            row_dict = row_json
            logger.info(f"Record {idx} (ID {row_dict['id']}):")
            logger.info(f"  Before: data type = {type(row_dict['data']).__name__}")
            
            row_dict = serialize_json_for_variant(row_dict)
            
            logger.info(f"  After:  data type = {type(row_dict['data']).__name__}")
            if isinstance(row_dict['data'], str):
                logger.info(f"  ✓ Successfully serialized to JSON string")
            logger.info()
            
        except Exception as e:
            logger.error(f"Record {idx} FAILED: {e}\n")
    
    cur.close()
    conn.close()
    
    logger.info("=" * 80)
    logger.info("CONCLUSION")
    logger.info("=" * 80)
    logger.info("\nIf serialization works above, the issue is likely:")
    logger.info("1. dlt is rejecting records due to schema inference issues")
    logger.info("2. Records are being silently dropped before the transformation")
    logger.info("3. Need to check dlt pipeline logs for actual error messages")
    logger.info("=" * 80 + "\n")

if __name__ == "__main__":
    test_serialization()
