#!/usr/bin/env python3
import psycopg2
import urllib.parse
from dotenv import load_dotenv
import os

load_dotenv()

conn = psycopg2.connect(
    host=os.getenv('DB_HOST'),
    port=os.getenv('DB_PORT', '5432'),
    database=os.getenv('DB_NAME'),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD')
)

cur = conn.cursor()

print("=" * 80)
print("ANALYZING DATA GAPS - AUDITLOG_LOGENTRY (648 missing records)")
print("=" * 80)

# Check for NULL values in key columns
cur.execute("""
SELECT 
    COUNT(*) as total,
    COUNT(CASE WHEN id IS NULL THEN 1 END) as null_ids,
    COUNT(CASE WHEN content_type_id IS NULL THEN 1 END) as null_ct_id,
    COUNT(CASE WHEN object_id IS NULL THEN 1 END) as null_object_id
FROM auditlog_logentry
""")

result = cur.fetchone()
print(f"\nTotal records: {result[0]}")
print(f"NULL id: {result[1]}")
print(f"NULL content_type_id: {result[2]}")
print(f"NULL object_id: {result[3]}")

# Check for problematic records
cur.execute("""
SELECT COUNT(DISTINCT id) FROM auditlog_logentry
""")
unique_ids = cur.fetchone()[0]
print(f"Unique record IDs: {unique_ids}")

# Check columns with potential issues
cur.execute("""
SELECT column_name, data_type 
FROM information_schema.columns 
WHERE table_name = 'auditlog_logentry' AND table_schema = 'public'
ORDER BY column_name
""")

print("\n=== Table Schema ===")
for col_name, col_type in cur.fetchall():
    print(f"  {col_name:30} | {col_type}")

# Check for long strings and JSONB columns
cur.execute("""
SELECT 
    COUNT(*) as total,
    COUNT(*) FILTER (WHERE changes IS NOT NULL) as records_with_changes_json,
    COUNT(*) FILTER (WHERE additional_data IS NOT NULL) as records_with_additional_data,
    COUNT(*) FILTER (WHERE serialized_data IS NOT NULL) as records_with_serialized_data,
    COALESCE(MAX(LENGTH(CAST(changes_text AS TEXT))), 0) as max_changes_text_len
FROM auditlog_logentry
""")

result = cur.fetchone()
if result:
    print(f"\n=== Column Analysis ===")
    print(f"Total records: {result[0]}")
    print(f"Records with changes JSON: {result[1]}")
    print(f"Records with additional_data JSON: {result[2]}")
    print(f"Records with serialized_data JSON: {result[3]}")
    print(f"Max changes_text length: {result[4]} chars")

# Check for problematic data that might fail conversion
cur.execute("""
SELECT 
    COUNT(*) FILTER (WHERE changes_text IS NULL) as null_changes_text,
    COUNT(*) FILTER (WHERE object_repr IS NULL) as null_object_repr,
    COUNT(*) FILTER (WHERE timestamp IS NULL) as null_timestamp,
    COUNT(*) FILTER (WHERE remote_addr IS NOT NULL AND remote_addr::text = '') as empty_remote_addr
FROM auditlog_logentry
""")

result = cur.fetchone()
if result:
    print(f"\nRecords with NULL changes_text: {result[0]}")
    print(f"Records with NULL object_repr: {result[1]}")
    print(f"Records with NULL timestamp: {result[2]}")
    print(f"Records with empty remote_addr: {result[3]}")

conn.close()
print("\n" + "=" * 80)
