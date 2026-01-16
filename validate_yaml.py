import yaml
try:
    with open('bitbucket-pipelines.yml') as f:
        yaml.safe_load(f)
    print('✓ YAML valid')
except Exception as e:
    print(f'✗ Error: {e}')
