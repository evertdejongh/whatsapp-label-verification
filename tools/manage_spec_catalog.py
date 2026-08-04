#!/usr/bin/env python3
"""
CRUD utility for the whatsapp-spec-catalog DynamoDB table (spec_code ->
description, pdf_url, pdf_s3_key). Complements tools/sync_spec_catalog.py,
which does a full bulk re-sync from the Dole spreadsheet -- this script is
for one-off inserts/edits/deletes without re-running the whole batch.

Run this in AWS CloudShell (region us-east-2), not on a local machine --
local AWS CLI/boto3 calls don't reach AWS from this dev machine.

Run with no arguments for an interactive menu:
  python3 manage_spec_catalog.py

Or use it non-interactively:
  python3 manage_spec_catalog.py list
  python3 manage_spec_catalog.py get 9A
  python3 manage_spec_catalog.py put 9A --description "LIDL EU | 4.5kg Generic Paper Bag | 2026 | V1.0" --pdf-url "https://dl.d6.co.za/.../9a_....pdf"
  python3 manage_spec_catalog.py put 9A --description "New description only, keep existing PDF"
  python3 manage_spec_catalog.py delete 9A
  python3 manage_spec_catalog.py delete 9A --delete-pdf
"""

import argparse
import sys
import urllib.request
from types import SimpleNamespace

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-2"
BUCKET_NAME = "dole-pallet-specs-2026-677513501349-us-east-2-an"
TABLE_NAME = "whatsapp-spec-catalog"

s3 = boto3.client("s3", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)


def download_pdf(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def cmd_list(args):
    resp = table.scan()
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))

    if not items:
        print("(table is empty)")
        return

    for item in sorted(items, key=lambda i: i["spec_code"]):
        print(f"{item['spec_code']:<8} {item.get('description', ''):<70} {item.get('pdf_s3_key', '(no pdf)')}")
    print(f"{len(items)} spec(s) total.")


def cmd_get(args):
    spec_code = args.spec_code.upper()
    res = table.get_item(Key={"spec_code": spec_code})
    item = res.get("Item")
    if not item:
        print(f"No entry for spec_code={spec_code}")
        sys.exit(1)
    for k, v in item.items():
        print(f"{k}: {v}")


def cmd_put(args):
    spec_code = args.spec_code.upper()

    existing = table.get_item(Key={"spec_code": spec_code}).get("Item") or {}

    description = args.description if args.description is not None else existing.get("description", "")
    pdf_url = args.pdf_url if args.pdf_url is not None else existing.get("pdf_url")
    pdf_s3_key = existing.get("pdf_s3_key")

    if args.pdf_url is not None:
        s3_key = f"specs/{spec_code}_specsheet.pdf"
        print(f"Downloading PDF from {pdf_url} ...")
        pdf_bytes = download_pdf(pdf_url)
        print(f"Uploading to s3://{BUCKET_NAME}/{s3_key} ({len(pdf_bytes)} bytes) ...")
        s3.put_object(Bucket=BUCKET_NAME, Key=s3_key, Body=pdf_bytes, ContentType="application/pdf")
        pdf_s3_key = s3_key

    item = {"spec_code": spec_code, "description": description}
    if pdf_url:
        item["pdf_url"] = pdf_url
    if pdf_s3_key:
        item["pdf_s3_key"] = pdf_s3_key

    table.put_item(Item=item)
    print(f"Saved spec_code={spec_code}:")
    for k, v in item.items():
        print(f"  {k}: {v}")


def cmd_delete(args):
    spec_code = args.spec_code.upper()
    existing = table.get_item(Key={"spec_code": spec_code}).get("Item")
    if not existing:
        print(f"No entry for spec_code={spec_code} (nothing to delete)")
        return

    table.delete_item(Key={"spec_code": spec_code})
    print(f"Deleted DynamoDB entry for spec_code={spec_code}")

    if args.delete_pdf and existing.get("pdf_s3_key"):
        try:
            s3.delete_object(Bucket=BUCKET_NAME, Key=existing["pdf_s3_key"])
            print(f"Deleted s3://{BUCKET_NAME}/{existing['pdf_s3_key']}")
        except ClientError as e:
            print(f"Failed to delete S3 object: {e}")


def prompt(label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{label}{suffix}: ").strip()
    return value if value else default


def interactive_menu():
    print("=== whatsapp-spec-catalog manager ===")
    while True:
        print(
            "\n1) List all specs\n"
            "2) Get one spec\n"
            "3) Put (insert/update) a spec\n"
            "4) Delete a spec\n"
            "5) Quit"
        )
        choice = input("Choose an option: ").strip()

        try:
            if choice == "1":
                cmd_list(SimpleNamespace())
            elif choice == "2":
                spec_code = prompt("Spec code")
                if not spec_code:
                    continue
                cmd_get(SimpleNamespace(spec_code=spec_code))
            elif choice == "3":
                spec_code = prompt("Spec code")
                if not spec_code:
                    continue
                description = prompt("Description (blank = keep existing)", default=None)
                pdf_url = prompt("PDF source URL (blank = keep existing)", default=None)
                cmd_put(SimpleNamespace(spec_code=spec_code, description=description, pdf_url=pdf_url))
            elif choice == "4":
                spec_code = prompt("Spec code")
                if not spec_code:
                    continue
                delete_pdf = prompt("Also delete cached PDF from S3? (y/N)", default="n").lower().startswith("y")
                cmd_delete(SimpleNamespace(spec_code=spec_code, delete_pdf=delete_pdf))
            elif choice == "5":
                break
            else:
                print("Not a valid option, try again.")
        except Exception as e:
            print(f"Error: {e}")


def main():
    if len(sys.argv) == 1:
        interactive_menu()
        return

    parser = argparse.ArgumentParser(description="Manage the whatsapp-spec-catalog DynamoDB table")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all spec catalog entries").set_defaults(func=cmd_list)

    p_get = sub.add_parser("get", help="Show one spec catalog entry")
    p_get.add_argument("spec_code")
    p_get.set_defaults(func=cmd_get)

    p_put = sub.add_parser("put", help="Insert or update a spec catalog entry")
    p_put.add_argument("spec_code")
    p_put.add_argument("--description", help="Description text (omit to keep existing)")
    p_put.add_argument("--pdf-url", help="Source PDF URL to (re-)download and cache in S3 (omit to keep existing PDF)")
    p_put.set_defaults(func=cmd_put)

    p_del = sub.add_parser("delete", help="Delete a spec catalog entry")
    p_del.add_argument("spec_code")
    p_del.add_argument("--delete-pdf", action="store_true", help="Also delete the cached PDF from S3")
    p_del.set_defaults(func=cmd_delete)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
