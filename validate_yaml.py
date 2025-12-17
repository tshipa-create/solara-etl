#!/usr/bin/env python3
import yaml
import sys

def constructor_for_cloudformation_tags(loader, tag_suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    elif isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    else:
        return loader.construct_scalar(node)

try:
    with open('lambda_deploy.yaml', 'r') as f:
        loader = yaml.SafeLoader
        loader.add_multi_constructor('!', constructor_for_cloudformation_tags)
        yaml.load(f, Loader=loader)
    print("lambda_deploy.yaml: OK")
    sys.exit(0)
except Exception as e:
    print(f"Error validating YAML: {e}")
    sys.exit(1)
