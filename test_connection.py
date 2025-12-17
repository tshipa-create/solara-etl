#!/usr/bin/env python3

import psycopg2
import os
import sys

def test_postgres():
    try:
        conn = psycopg2.connect(
            host=os.getenv('DB_HOST'),
            port='5432',
            database=os.getenv('DB_NAME'),
            user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD')
        )
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM pg_tables WHERE schemaname=\'public\'')
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        print(f'✓ PostgreSQL: {count} tables found')
        return True
    except Exception as e:
        print(f'✗ PostgreSQL connection failed: {e}')
        return False

if __name__ == '__main__':
    if not test_postgres():
        sys.exit(1)
    print('Connection test passed!')
