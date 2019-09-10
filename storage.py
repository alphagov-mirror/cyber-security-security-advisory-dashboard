import os
import sys
import json

import boto3
from addict import Dict


REGION = None
OPTIONS = None


def set_region(region):
    global REGION
    REGION = region


def get_options():
    global OPTIONS
    print("GET:" + str(OPTIONS), sys.stderr)
    return OPTIONS


def set_options(options):
    global OPTIONS
    OPTIONS = Dict(options)
    print("SET: " + str(OPTIONS), sys.stderr)


def save_json(path, content):
    json_content = json.dumps(content, indent=2)
    return save(path, json_content)


def save(path, content):
    options = get_options()
    if options.type == "local":
        print("Saving to local file system", sys.stderr)
        relative_path = os.path.join(OPTIONS.location, path)
        status = save_local(relative_path, content)
    else:
        print("Saving to S3 bucket", sys.stderr)
        status = save_s3(path, content)
    return status


def read_json(path):
    content = read(path)
    parsed = Dict(json.loads(content))
    return parsed


def read(path):
    options = get_options()
    if options.type == "local":
        print("Reading from local file system", sys.stderr)
        content = read_local(path)
    else:
        print("Reading from S3 bucket", sys.stderr)
        content = read_s3(path)
    return content


# Save and read from local file system
def save_local(path, content):
    print(f"Path: {path}", sys.stderr)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        status = True
    except Exception:
        status = False
    return status


def read_local(path):
    try:
        relative_path = os.path.join(OPTIONS.location, path)
        with open(relative_path, "r") as f:
            content = f.read()
        status = True
    except Exception:
        status = False
        content = ""
    return content


# Save and read from S3 bucket
def get_s3_client():
    if "AWS_SECRET_ACCESS_KEY" in os.environ:
        s3 = boto3.client(
            "s3",
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            aws_session_token=os.environ["AWS_SESSION_TOKEN"],
            region_name=OPTIONS.region,
        )
    else:
        s3 = None
    return s3


def save_s3(path, content):
    try:
        s3 = get_s3_client()
        bytes_content = str.encode(content)
        bucket_name = OPTIONS.location
        response = s3.put_object(Bucket=bucket_name, Key=path, Body=bytes_content)
        tag = response["ETag"]
        print(f"Saved object tag: {tag} to bucket")
        status = True
    except Exception as err:
        # TODO error handling
        print(str(err))
        status = False
    return status


def read_s3(path):
    try:
        s3 = get_s3_client()
        response = s3.get_object(Bucket=OPTIONS.location, Key=path)
        bytes_content = response["Body"].read()
        content = bytes_content.decode()
    except Exception as err:
        # TODO error handling
        print(str(err))
        content = ""

    return content