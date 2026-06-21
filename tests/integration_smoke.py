import hashlib
import json
import os
import sys
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value is not None and value != "" else default


def build_client():
    endpoint = _env("S3_ENDPOINT_URL", "http://127.0.0.1:4431")
    access_key = _env("S3_ACCESS_KEY", "s3admin")
    secret_key = _env("S3_SECRET_KEY", "12345678")
    region = _env("S3_REGION", "us-east-1")
    addressing_style = _env(
        "S3_ADDRESSING_STYLE", "path"
    )  # path-style for custom server
    verify_tls = _env("S3_VERIFY_TLS", "false").lower() in {"1", "true", "yes", "on"}

    session = boto3.session.Session()
    client = session.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        verify=verify_tls,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )
    return client


def assert_true(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)


def debug_head_object(head_obj: dict, label: str = "HeadObject"):
    metadata = head_obj.get("Metadata", {})
    response_headers = head_obj.get("ResponseMetadata", {}).get("HTTPHeaders", {}) or {}
    key_headers = {
        k: v
        for k, v in response_headers.items()
        if k.lower().startswith("x-amz-meta-")
        or k.lower() in {"etag", "last-modified", "content-length", "accept-ranges"}
    }

    print(f"[DEBUG] {label} Metadata (full):")
    print(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True))

    print(f"[DEBUG] {label} Key Response Headers:")
    print(json.dumps(key_headers, ensure_ascii=False, indent=2, sort_keys=True))


def print_step(name: str):
    print(f"\n=== {name} ===")


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def expect_client_error(fn, expected_code: str):
    try:
        fn()
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        assert_true(
            code == expected_code,
            f"Expected error code {expected_code}, got {code}. Full error: {e}",
        )
        print(f"[OK] got expected error: {expected_code}")
        return
    raise AssertionError(f"Expected ClientError({expected_code}), but call succeeded")


def main():
    client = build_client()

    suffix = uuid.uuid4().hex[:8]
    bucket = f"smoke-{suffix}"
    bucket2 = f"smoke2-{suffix}"

    key = "dir/a.txt"
    key2 = "dir/b.txt"
    copy_key = "copied/a-copy.txt"

    body = b"hello-s3-smoke-test-" + uuid.uuid4().hex.encode("utf-8")
    body2 = b"second-object-" + uuid.uuid4().hex.encode("utf-8")

    meta = {
        "author": "integration-test",
        "trace-id": uuid.uuid4().hex,
    }

    print("S3 smoke test config:")
    print(
        json.dumps(
            {
                "endpoint": _env("S3_ENDPOINT_URL", "http://127.0.0.1:4431"),
                "region": _env("S3_REGION", "us-east-1"),
                "access_key": _env("S3_ACCESS_KEY", "s3admin"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    # 1) Create buckets
    print_step("CreateBucket")
    client.create_bucket(Bucket=bucket)
    client.create_bucket(Bucket=bucket2)
    print(f"[OK] created buckets: {bucket}, {bucket2}")

    # 2) Put object with metadata
    print_step("PutObject with Metadata")
    put_resp = client.put_object(Bucket=bucket, Key=key, Body=body, Metadata=meta)
    etag_put = (put_resp.get("ETag") or "").strip('"')
    assert_true(etag_put == md5_hex(body), "ETag mismatch for put_object")
    print(f"[OK] put {bucket}/{key}, etag={etag_put}")

    # 3) HeadObject and validate metadata
    print_step("HeadObject")
    head = client.head_object(Bucket=bucket, Key=key)
    got_meta = head.get("Metadata", {})
    got_meta_ci = {str(k).lower(): v for k, v in got_meta.items()}
    if got_meta_ci.get("author") != meta["author"]:
        debug_head_object(head, "HeadObject(main)")
        raise AssertionError("Metadata author mismatch")
    if got_meta_ci.get("trace-id") != meta["trace-id"]:
        debug_head_object(head, "HeadObject(main)")
        raise AssertionError("Metadata trace-id mismatch")
    print(f"[OK] head metadata={got_meta}")

    # 4) Conditional GET - IfNoneMatch should return 304
    print_step("Conditional GET (IfNoneMatch)")
    try:
        client.get_object(Bucket=bucket, Key=key, IfNoneMatch=f'"{etag_put}"')
        raise AssertionError("Expected 304 Not Modified, but get_object succeeded")
    except ClientError as e:
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        assert_true(status == 304, f"Expected HTTP 304, got {status}. Error: {e}")
        print("[OK] got expected 304 Not Modified")

    # 5) Full GET
    print_step("GetObject full")
    get_resp = client.get_object(Bucket=bucket, Key=key)
    got = get_resp["Body"].read()
    assert_true(got == body, "Downloaded body mismatch")
    print(f"[OK] full get matched ({len(got)} bytes)")

    # 6) Range GET
    print_step("GetObject range")
    r = client.get_object(Bucket=bucket, Key=key, Range="bytes=0-4")
    part = r["Body"].read()
    assert_true(
        part == body[:5], f"Range body mismatch: expected {body[:5]!r}, got {part!r}"
    )
    print(f"[OK] range get matched ({part!r})")

    # 7) Additional object for pagination
    print_step("PutObject second key")
    client.put_object(Bucket=bucket, Key=key2, Body=body2)
    print(f"[OK] put {bucket}/{key2}")

    # 8) ListObjectsV2 with pagination
    print_step("ListObjectsV2 pagination")
    page1 = client.list_objects_v2(Bucket=bucket, Prefix="dir/", MaxKeys=1)
    keys1 = [x["Key"] for x in page1.get("Contents", [])]
    assert_true(len(keys1) == 1, f"Expected 1 key on page1, got {len(keys1)}")
    assert_true(page1.get("IsTruncated") is True, "Expected IsTruncated=True on page1")
    token = page1.get("NextContinuationToken")
    assert_true(bool(token), "Expected NextContinuationToken on page1")
    print(f"[OK] page1 keys={keys1} token={token}")

    page2 = client.list_objects_v2(
        Bucket=bucket, Prefix="dir/", MaxKeys=10, ContinuationToken=token
    )
    keys2 = [x["Key"] for x in page2.get("Contents", [])]
    assert_true(len(keys2) >= 1, "Expected at least 1 key on page2")
    all_keys = sorted(keys1 + keys2)
    assert_true(
        "dir/a.txt" in all_keys and "dir/b.txt" in all_keys,
        f"Unexpected keys: {all_keys}",
    )
    print(f"[OK] page2 keys={keys2}")

    # 9) CopyObject with MetadataDirective=COPY
    print_step("CopyObject MetadataDirective=COPY")
    copy_resp = client.copy_object(
        Bucket=bucket2,
        Key=copy_key,
        CopySource={"Bucket": bucket, "Key": key},
        MetadataDirective="COPY",
    )
    copy_etag = copy_resp.get("CopyObjectResult", {}).get("ETag", "").strip('"')
    assert_true(copy_etag == etag_put, "Copy ETag mismatch with source")
    head_copy = client.head_object(Bucket=bucket2, Key=copy_key)
    meta_copy = head_copy.get("Metadata", {})
    meta_copy_ci = {str(k).lower(): v for k, v in meta_copy.items()}
    if meta_copy_ci.get("author") != meta["author"]:
        debug_head_object(head_copy, "HeadObject(copy-copy)")
        raise AssertionError("COPY should preserve metadata author")
    if meta_copy_ci.get("trace-id") != meta["trace-id"]:
        debug_head_object(head_copy, "HeadObject(copy-copy)")
        raise AssertionError("COPY should preserve metadata trace-id")
    print(f"[OK] copy(COPY) metadata={meta_copy}")

    # 10) CopyObject with MetadataDirective=REPLACE
    print_step("CopyObject MetadataDirective=REPLACE")
    replaced_meta = {"author": "replaced", "newkey": "yes"}
    replace_key = "copied/a-replace.txt"
    client.copy_object(
        Bucket=bucket2,
        Key=replace_key,
        CopySource={"Bucket": bucket, "Key": key},
        MetadataDirective="REPLACE",
        Metadata=replaced_meta,
    )
    head_replaced = client.head_object(Bucket=bucket2, Key=replace_key)
    meta_replaced = head_replaced.get("Metadata", {})
    meta_replaced_ci = {str(k).lower(): v for k, v in meta_replaced.items()}
    if meta_replaced_ci.get("author") != "replaced":
        debug_head_object(head_replaced, "HeadObject(copy-replace)")
        raise AssertionError("REPLACE metadata author mismatch")
    if meta_replaced_ci.get("newkey") != "yes":
        debug_head_object(head_replaced, "HeadObject(copy-replace)")
        raise AssertionError("REPLACE metadata newkey mismatch")
    if "trace-id" in meta_replaced_ci:
        debug_head_object(head_replaced, "HeadObject(copy-replace)")
        raise AssertionError("REPLACE should not keep old metadata")
    print(f"[OK] copy(REPLACE) metadata={meta_replaced}")

    # 11) DeleteObjects (batch delete)
    print_step("DeleteObjects batch delete")
    batch_keys = ["batch/a.txt", "batch/b.txt", "batch/c.txt"]
    for bk in batch_keys:
        client.put_object(Bucket=bucket, Key=bk, Body=b"batch-test")
    # Verify 3 objects exist
    list_before = client.list_objects_v2(Bucket=bucket, Prefix="batch/")
    assert_true(
        len(list_before.get("Contents", [])) == 3,
        f"Expected 3 batch objects, got {len(list_before.get('Contents', []))}",
    )
    # Batch delete all 3
    delete_objs = [{"Key": bk} for bk in batch_keys]
    del_resp = client.delete_objects(Bucket=bucket, Delete={"Objects": delete_objs})
    assert_true(
        len(del_resp.get("Deleted", [])) == 3,
        f"Expected 3 deleted, got {len(del_resp.get('Deleted', []))}",
    )
    errors = del_resp.get("Errors", [])
    assert_true(len(errors) == 0, f"Expected 0 errors, got {len(errors)}: {errors}")
    # Verify they're gone
    list_after = client.list_objects_v2(Bucket=bucket, Prefix="batch/")
    assert_true(
        len(list_after.get("Contents", [])) == 0,
        f"Expected 0 batch objects after delete, got {len(list_after.get('Contents', []))}",
    )
    # Test quiet mode with non-existent keys (idempotent)
    del_quiet = client.delete_objects(
        Bucket=bucket,
        Delete={"Objects": [{"Key": "nonexistent1"}, {"Key": "nonexistent2"}], "Quiet": True},
    )
    assert_true(len(del_quiet.get("Deleted", [])) == 2, "Quiet mode should report deletes")
    assert_true(len(del_quiet.get("Errors", [])) == 0, "Quiet mode should have no errors")
    print(f"[OK] DeleteObjects batch={batch_keys} quiet=2")

    # 12) Delete object idempotency
    print_step("DeleteObject idempotent")
    client.delete_object(Bucket=bucket, Key="not-exists.txt")
    client.delete_object(Bucket=bucket, Key=key)
    print("[OK] delete object calls succeeded")

    # 13) Delete non-empty bucket should fail
    print_step("DeleteBucket non-empty should fail")
    expect_client_error(lambda: client.delete_bucket(Bucket=bucket), "BucketNotEmpty")

    # 14) Cleanup
    print_step("Cleanup")
    # bucket
    client.delete_object(Bucket=bucket, Key=key2)
    client.delete_bucket(Bucket=bucket)
    # bucket2
    client.delete_object(Bucket=bucket2, Key=copy_key)
    client.delete_object(Bucket=bucket2, Key=replace_key)
    client.delete_bucket(Bucket=bucket2)
    print("[OK] cleanup completed")

    print("\n[PASS] Integration smoke test passed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n[FAIL] Integration smoke test failed: {e}")
        sys.exit(1)
