#!/usr/bin/env python3
import sys
import ast

try:
    with open('main.py', 'r') as f:
        code = f.read()
    
    tree = ast.parse(code)
    
    has_lambda_handler = False
    has_run_pipeline = False
    
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name == 'lambda_handler':
                has_lambda_handler = True
            if node.name == 'run_pipeline':
                has_run_pipeline = True
    
    if has_lambda_handler:
        print("OK: lambda_handler function found")
    else:
        print("ERROR: lambda_handler function not found")
        sys.exit(1)
    
    if has_run_pipeline:
        print("OK: run_pipeline function found")
    else:
        print("ERROR: run_pipeline function not found")
        sys.exit(1)
    
    print("OK: All checks passed")
    sys.exit(0)

except SyntaxError as e:
    print(f"SYNTAX ERROR in main.py: {e}")
    sys.exit(1)
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)
