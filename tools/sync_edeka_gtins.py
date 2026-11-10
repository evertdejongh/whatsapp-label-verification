#!/usr/bin/env python3
"""
Loads the whatsapp-edeka-gtins DynamoDB table (EDEKA's own punnet/carton
GTINs for the 8* spec range) from EdekaGTINs.xlsx in the repo root.

Each source row identifies one EDEKA SKU by VarietyGroupCode + VarietyCode
(often blank -- EDEKA doesn't always care which specific variety within a
group) + InventoryCode + Pack, and carries that SKU's punnet GTIN and/or
carton GTIN (either can be blank -- not every SKU ships both pack types).
Neither GTIN column is usable as a DynamoDB hash key on its own (both can be
blank, and the webhook doesn't do a keyed GetItem against this table --
see lambda_function.py's lookup_rules "scan_match": a punnet label prints
no Pack/InventoryCode to key off of, only the barcode and the variety name,
so the engine scans the whole table matching GTINpunnet/GTINcarton +
VarietyGroupCode instead). The synthetic "lookup_key" here exists only to
satisfy DynamoDB's hash-key requirement and give the dashboard's Reference
Tables tab a stable row identity -- same role as IAN Numbers' lookup_key,
but unlike that table, nothing ever does a GetItem against it.

Safe to re-run whenever EdekaGTINs.xlsx changes -- existing rows are
overwritten by lookup_key, nothing is deleted if a row is removed from the
sheet (same caveat as the other sync scripts).

Usage:
    python tools/sync_edeka_gtins.py
"""
import os

import boto3
from botocore.exceptions import ClientError
from openpyxl import load_workbook

REGION = "us-east-2"
TABLE_NAME = "whatsapp-edeka-gtins"
XLSX_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "EdekaGTINs.xlsx")

dynamodb = boto3.resource("dynamodb", region_name=REGION)


def ensure_table():
    client = dynamodb.meta.client
    try:
        client.describe_table(TableName=TABLE_NAME)
        print(f"Table {TABLE_NAME} already exists.")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    print(f"Creating table {TABLE_NAME}...")
    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "lookup_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "lookup_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)
    print(f"Table {TABLE_NAME} created.")


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def main():
    ensure_table()
    table = dynamodb.Table(TABLE_NAME)

    wb = load_workbook(XLSX_PATH, data_only=True)
    ws = wb["EdekaGTINs"]
    rows = list(ws.iter_rows(values_only=True))
    header, data_rows = rows[0], rows[1:]
    # Varietygroup, Varietycode, Sizecount, Invcode, Brand, Pack, Gtinpunnet, Gtincarton
    col = {name: i for i, name in enumerate(header)}

    count, skipped = 0, 0
    with table.batch_writer(overwrite_by_pkeys=["lookup_key"]) as batch:
        for row in data_rows:
            variety_group = clean(row[col["Varietygroup"]]).upper()
            variety_code = clean(row[col["Varietycode"]]).upper()
            inv_code = clean(row[col["Invcode"]]).upper()
            pack = clean(row[col["Pack"]]).upper()
            gtin_punnet = clean(row[col["Gtinpunnet"]])
            gtin_carton = clean(row[col["Gtincarton"]])

            if not variety_group or not inv_code or not pack:
                skipped += 1
                continue
            if not gtin_punnet and not gtin_carton:
                skipped += 1
                continue

            item = {
                "lookup_key": "#".join([variety_group, variety_code, inv_code, pack]),
                "VarietyGroupCode": variety_group,
                "InventoryCode": inv_code,
                "Pack": pack,
            }
            if variety_code:
                item["VarietyCode"] = variety_code
            brand = clean(row[col["Brand"]])
            if brand:
                item["Brand"] = brand
            size_count = row[col["Sizecount"]]
            if size_count not in (None, ""):
                item["SizeCount"] = str(size_count)
            if gtin_punnet:
                item["GTINpunnet"] = gtin_punnet
            if gtin_carton:
                item["GTINcarton"] = gtin_carton

            batch.put_item(Item=item)
            count += 1

    print(f"{TABLE_NAME}: loaded {count} rows, skipped {skipped} (missing key or both GTINs blank).")


if __name__ == "__main__":
    main()
