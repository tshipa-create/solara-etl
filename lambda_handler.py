import sys
import json
from main import run

def lambda_handler(event, context):
    try:
        run(num_workers=4, batch_size=5000)
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "ETL pipeline completed successfully"})
        }
    except Exception as e:
        print(f"ETL pipeline failed: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e)})
        }
