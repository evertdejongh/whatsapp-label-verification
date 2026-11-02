"""
Loads the Variety / VarietyGroup / IANNumbers / REWE-combinations / agent-
address reference tables (used for cross-checking scanned label values, e.g.
PUC -> GGN) into DynamoDB. Variety, VarietyGroup, IANNumbers and the agent
addresses are read directly from their live Clarion TPS files; REWE
combinations are hardcoded (see the comment above REWE_COMBINATION_ROWS).
whatsapp-puc and whatsapp-commodity are deliberately NOT handled here --
see the comment above sync_rewe_combinations() for why. Idempotent/safe to
re-run whenever the underlying Clarion data changes -- same pattern as
sync_spec_catalog.py.

Usage:
    python tools/sync_reference_tables.py
"""
import re
import os

import boto3
from botocore.exceptions import ClientError
from striprtf.striprtf import rtf_to_text

from tpsread import TPS


def normalize_key_text(value):
    """
    Collapses whitespace runs to a single space before upper-casing, matching
    the normalization the webhook applies to values it transcribes off a
    label (which can carry a line-wrap newline where this source data has a
    plain space) -- keeps stored keys and lookup keys on the same footing.
    """
    return re.sub(r"\s+", " ", str(value)).strip().upper()


def clean_tps_field(value):
    """
    TPS STRING fields are fixed-width and null-padded at rest -- a raw read
    carries that padding through as trailing \\x00 bytes, which plain
    .strip() doesn't touch. Every TPS field value needs this before use.
    """
    if value is None:
        return ""
    return str(value).rstrip("\x00").strip()


def clean_tps_rtf_field(value):
    """
    Same null-padding strip as clean_tps_field(), plus RTF->plain-text
    extraction for the handful of TPS fields stored as RTF blobs (Clarion's
    TEXT,RTF(TEXT:FIELD) fields -- e.g. VG:LIDLDESCRIPTION). Raw RTF control
    codes would never match real label text if a field like this is ever
    used for matching, so this is the form worth storing.
    """
    text = clean_tps_field(value)
    if not text:
        return ""
    return rtf_to_text(text).strip()


REGION = "us-east-2"

# Customers.TPS and InvCode.TPS are the live Clarion tables that Agents.xlsx
# and extracted_indian_addresses.xlsx used to be one-time manual exports of
# (confirmed by field-name lineage -- InvCode's INV:INDIANADDRESS3 is
# literally where extracted_indian_addresses.xlsx's oddly-spaced
# "Inv: IndianAddress3" header came from). Reading them directly here means
# this script tracks the live DoleLabels data instead of a snapshot someone
# has to remember to re-export.
TPS_DATA_DIR = r"C:\Clarion 12 Source\DoleLabels\Data"
CUSTOMERS_TPS_PATH = os.path.join(TPS_DATA_DIR, "Customers.TPS")
INVCODE_TPS_PATH = os.path.join(TPS_DATA_DIR, "InvCode.TPS")
VARIETIES_TPS_PATH = os.path.join(TPS_DATA_DIR, "Varieties.TPS")
VARGRPS_TPS_PATH = os.path.join(TPS_DATA_DIR, "VarGrps.TPS")
IAN_TPS_PATH = os.path.join(TPS_DATA_DIR, "ian.TPS")
# Commodities.TPS was tried and dropped: tpsread can't parse it (its header
# carries what looks like an offset array none of the single-table files
# here have, suggesting multiple logical tables packed into the one
# physical file). Moot anyway -- whatsapp-commodity changes rarely enough
# that the user maintains it by hand via the dashboard's Reference Tables
# tab instead, same as whatsapp-puc (owned by a separate scheduled job).
# Neither is synced by this script; see the note above sync_rewe_combinations().
TPS_ENCODING = "cp1252"

VARIETY_TABLE = "whatsapp-variety"
VARIETY_GROUP_TABLE = "whatsapp-variety-group"
IAN_NUMBERS_TABLE = "whatsapp-ian-numbers"
REWE_COMBINATIONS_TABLE = "whatsapp-rewe-combinations"
AGENT_ADDRESSES_TABLE = "whatsapp-agent-addresses"

# Agent/importer codes are unique across every country that uses this table
# (confirmed by the user), so one shared table + one flat "Code" key covers
# all of them -- no need for a per-country namespace. Non-India rows come
# from tools/Agents.xlsx (see sync_general_agents()) rather than being
# hardcoded, now that a real source spreadsheet exists for them.
#
# One genuine cross-country code collision was found and resolved by the
# user 2026-09-10: code "XX" existed in the India sheet as a duplicate of
# "TA" (same company "SAMRIDDHI TRADE", same FSSAI -- a data-entry error in
# the original India source, not a real second consignee) and *separately*
# in Agents.xlsx as a real, different company ("PT. PARAHYANGAN SUKSES
# FOODS", Indonesia). Resolution: the India loader below explicitly skips
# "XX" (keeping only "TA" for Samriddhi Trade); Agents.xlsx's "XX" is the
# real occupant of that code going forward.
INDIA_DUPLICATE_CODES_TO_SKIP = {"XX"}

# REWE (13A/13B/13C/13D) rule 6: which GTIN/supplier-code/bilingual-name/
# packaging text is correct depends on Pack + VarietyGroup, except the
# BX/RX/3X variety groups which also depend on InventoryCode. Rather than
# teach the lookup engine that bucketing logic, every simple combination is
# stored under a 2-part key ("Pack#VarietyGroup") and only the one
# InvCode='RH' exception gets a 3-part key ("Pack#VarietyGroup#InvCode") --
# the webhook tries the 3-part key first, falling back to the 2-part one
# (see lambda_function.py's lookup_rules "fallback_key"). Hardcoded directly
# from the user's rules (no spreadsheet source), same pattern as
# sync_spec_catalog.py's embedded SPEC_ROWS.
REWE_COMBINATION_ROWS = []


def _add_rewe_rows(packs, variety_group, nan_puc, gtin, de, en, packaging, inv_code=None):
    for pack in packs:
        key_parts = [pack, variety_group] + ([inv_code] if inv_code else [])
        REWE_COMBINATION_ROWS.append({
            "lookup_key": "#".join(key_parts),
            "ReweNanPUC": nan_puc,
            "GTIN": gtin,
            "VarietyGroupDE": de,
            "VarietyGroupEN": en,
            "REWEPackaging": packaging,
        })


# Pack A04I / B04I bucket (4.5kg)
# ReweNanPUC suffix updated 2026-10-01 from D7520 to E4711 (rebuilt from the
# current Clarion LidlPunnetStonefruit embed source the user pasted -- GTINs/
# DE/EN names/packaging all confirmed unchanged, only this suffix moved).
_add_rewe_rows(["A04I", "B04I"], "WS", "7722245 - E4711", "4337256546317", "Trauben Hell Kernlos", "White Seedless", "4.5kg")
_add_rewe_rows(["A04I", "B04I"], "RS", "8873554 - E4711", "4337256562003", "Trauben Rot Kernlos", "Red Seedless", "4.5kg")
_add_rewe_rows(["A04I", "B04I"], "BS", "8952160 - E4711", "4337256558372", "Trauben Blau Kernlos", "Black Seedless", "4.5kg")

# Pack A05D / A05A bucket (10x500g)
_add_rewe_rows(["A05D", "A05A"], "WS", "7412355 - E4711", "4046118840691", "Trauben Hell Kernlos", "White Seedless", "10x500g")
_add_rewe_rows(["A05D", "A05A"], "RS", "9232885 - E4711", "4051826535715", "Trauben Dunkel Kernlos", "Red Seedless", "10x500g")
_add_rewe_rows(["A05D", "A05A"], "BS", "9232885 - E4711", "4051826535715", "Trauben Dunkel Kernlos", "Black Seedless", "10x500g")
# BX/RX/3X default (InventoryCode != 'RH')
for _vg in ("BX", "RX", "3X"):
    _add_rewe_rows(["A05D", "A05A"], _vg, "377024 - E4711", "4051826598116", "Trauben Mix Kernlos", "Mixed Varieties", "10x500g")
# BX/RX/3X exception (InventoryCode == 'RH')
for _vg in ("BX", "RX", "3X"):
    _add_rewe_rows(
        ["A05D", "A05A"], _vg, "9001853 - E4711", "4337256718363",
        "Rewe Beste Wahl Tafeltrauben Mix, kernlos", "Mixed Varieties", "10x500g", inv_code="RH",
    )

dynamodb = boto3.resource("dynamodb", region_name=REGION)


def ensure_table(table_name, key_schema, attribute_definitions):
    client = dynamodb.meta.client
    try:
        client.describe_table(TableName=table_name)
        print(f"Table {table_name} already exists.")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    print(f"Creating table {table_name}...")
    client.create_table(
        TableName=table_name,
        KeySchema=key_schema,
        AttributeDefinitions=attribute_definitions,
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=table_name)
    print(f"Table {table_name} created.")


def sync_variety():
    # VarietyName isn't globally unique on its own (a handful of names are
    # reused across different commodities, e.g. "EUREKA" is both a Blueberry
    # and a Lemon variety) -- Commodity is the range key so both rows survive
    # instead of one silently overwriting the other. Reads Varieties.TPS
    # directly (Clarion's live source) instead of WhatsAppTables.xlsx's
    # Variety sheet.
    ensure_table(
        VARIETY_TABLE,
        key_schema=[
            {"AttributeName": "VarietyName", "KeyType": "HASH"},
            {"AttributeName": "Commodity", "KeyType": "RANGE"},
        ],
        attribute_definitions=[
            {"AttributeName": "VarietyName", "AttributeType": "S"},
            {"AttributeName": "Commodity", "AttributeType": "S"},
        ],
    )
    table = dynamodb.Table(VARIETY_TABLE)
    tps = TPS(VARIETIES_TPS_PATH, encoding=TPS_ENCODING, cached=True, check=True, current_tablename="UNNAMED")
    count = 0
    with table.batch_writer(overwrite_by_pkeys=["VarietyName", "Commodity"]) as batch:
        for r in tps:
            variety_name = clean_tps_field(r.get("b'VAR:VARIETY'"))
            commodity = clean_tps_field(r.get("b'VAR:COMMODITY'"))
            if not variety_name or not commodity:
                continue
            batch.put_item(Item={
                "VarietyName": normalize_key_text(variety_name),
                "Commodity": normalize_key_text(commodity),
                "VarietyCode": clean_tps_field(r.get("b'VAR:CODE'")),
                "VarietyGroupCode": clean_tps_field(r.get("b'VAR:VARIETYGROUP'")),
            })
            count += 1
    print(f"{VARIETY_TABLE}: loaded {count} rows (from Varieties.TPS).")


def sync_variety_group():
    # VarietyGroupCode is confirmed globally unique (no two commodities reuse
    # the same code), so it's a clean single hash key. Reads VarGrps.TPS
    # directly. LidlTranslations isn't currently read by lambda_function.py
    # or the dashboard (checked both) -- kept for whatever it was meant for,
    # pulled from VG:LIDLDESCRIPTION's RTF via striprtf rather than stored as
    # raw RTF markup. VG:LIDLDESCRIPTION60MM (a second, smaller-label RTF
    # variant) isn't synced -- say if you want it added as its own field.
    ensure_table(
        VARIETY_GROUP_TABLE,
        key_schema=[{"AttributeName": "VarietyGroupCode", "KeyType": "HASH"}],
        attribute_definitions=[{"AttributeName": "VarietyGroupCode", "AttributeType": "S"}],
    )
    table = dynamodb.Table(VARIETY_GROUP_TABLE)
    tps = TPS(VARGRPS_TPS_PATH, encoding=TPS_ENCODING, cached=True, check=True, current_tablename="UNNAMED")
    count = 0
    with table.batch_writer(overwrite_by_pkeys=["VarietyGroupCode"]) as batch:
        for r in tps:
            code = clean_tps_field(r.get("b'VG:CODE'"))
            if not code:
                continue
            item = {
                "VarietyGroupCode": code,
                "Commodity": clean_tps_field(r.get("b'VG:COMMODITY'")).upper(),
            }
            lidl_translations = clean_tps_rtf_field(r.get("b'VG:LIDLDESCRIPTION'"))
            if lidl_translations:
                item["LidlTranslations"] = lidl_translations
            batch.put_item(Item=item)
            count += 1
    print(f"{VARIETY_GROUP_TABLE}: loaded {count} rows (from VarGrps.TPS).")


def sync_ian_numbers():
    # Four columns (Commodity, VarietyGroup, Pack, InventoryCode) make up the
    # real key -- DynamoDB only supports a hash+range key pair, so they're
    # concatenated into a single hash key rather than modeling a composite
    # range key for what's ultimately just an exact-match lookup. Reads
    # ian.TPS directly.
    ensure_table(
        IAN_NUMBERS_TABLE,
        key_schema=[{"AttributeName": "lookup_key", "KeyType": "HASH"}],
        attribute_definitions=[{"AttributeName": "lookup_key", "AttributeType": "S"}],
    )
    table = dynamodb.Table(IAN_NUMBERS_TABLE)
    tps = TPS(IAN_TPS_PATH, encoding=TPS_ENCODING, cached=True, check=True, current_tablename="UNNAMED")
    count = 0
    with table.batch_writer(overwrite_by_pkeys=["lookup_key"]) as batch:
        for r in tps:
            commodity = clean_tps_field(r.get("b'IAN:COMMODITY'")).upper()
            variety_group = clean_tps_field(r.get("b'IAN:VARIETYGROUP'")).upper()
            pack = clean_tps_field(r.get("b'IAN:PACK'")).upper()
            inventory_code = clean_tps_field(r.get("b'IAN:INVCODE'")).upper()
            ian_number = clean_tps_field(r.get("b'IAN:IAN'"))
            if not ian_number:
                continue
            batch.put_item(Item={
                "lookup_key": f"{commodity}#{variety_group}#{pack}#{inventory_code}",
                "Commodity": commodity,
                "VarietyGroup": variety_group,
                "Pack": pack,
                "InventoryCode": inventory_code,
                "IANNumber": ian_number,
            })
            count += 1
    print(f"{IAN_NUMBERS_TABLE}: loaded {count} rows (from ian.TPS).")


# whatsapp-puc and whatsapp-commodity are no longer synced by this script:
# PUC is already kept current by a separate scheduled job (confirmed with
# the user 2026-10-01 -- running this script's own version would just race
# it), and Commodity changes rarely enough that it's maintained by hand via
# the dashboard's Reference Tables tab instead. If either ever needs this
# script's help again, the old xlsx-reading implementations are in git
# history (both read from WhatsAppTables.xlsx's PUC/Commodity sheets, same
# ensure_table()/batch_writer() shape as the functions above).


def sync_rewe_combinations():
    ensure_table(
        REWE_COMBINATIONS_TABLE,
        key_schema=[{"AttributeName": "lookup_key", "KeyType": "HASH"}],
        attribute_definitions=[{"AttributeName": "lookup_key", "AttributeType": "S"}],
    )
    table = dynamodb.Table(REWE_COMBINATIONS_TABLE)
    count = 0
    with table.batch_writer(overwrite_by_pkeys=["lookup_key"]) as batch:
        for row in REWE_COMBINATION_ROWS:
            batch.put_item(Item=row)
            count += 1
    print(f"{REWE_COMBINATIONS_TABLE}: loaded {count} rows.")


def split_name_and_address(address1, *rest_lines):
    """
    Some source sheets run the company's full legal name straight into the
    first address line with no delimiter but a comma (e.g. "TENGINE SDN BHD,
    LOT 2457," or "SAHAJ LAXMI TRADERS, SHOP NO 23, GROUND FLOOR,"). Splitting
    at the first comma recovers a clean name for those rows -- the address
    fragment left over (if any) joins the rest of the address lines instead
    of staying glued to the name. Rows with no comma at all (Address1 is
    already just the name) are returned whole, unchanged. This matters
    because matching a name against the *whole* Address1 (name+address)
    would score low for a genuinely correct short name against a long
    combined string -- exactly the false-failure this split avoids. Shared
    by both the India and general-agents loaders below (same source-data
    shape, same fix).
    """
    name_part, _, addr1_remainder = address1.partition(",")
    importer_name = name_part.strip()
    address_lines = " ".join(p for p in [addr1_remainder.strip(), *rest_lines] if p)
    return importer_name, address_lines


def sync_agent_addresses():
    # Started as India-only (17C's consignee/agent code -> name, 3-line
    # address, FSSAI license number), generalized 2026-09-10 once 17A
    # (Malaysia) needed the exact same shape (agent code -> importer name +
    # address) -- renamed from whatsapp-india-addresses to this generic name.
    # Reads InvCode.TPS directly (Clarion's live source for this data, not a
    # manually re-exported snapshot) -- sync_general_agents() below loads the
    # non-India rows from Customers.TPS into the same table.
    #
    # Only ImporterName/AddressLines (the split results) and FSSAI are
    # actually referenced by any validation config -- the raw address
    # components aren't kept in the table at all, deliberately: this table is
    # hand-editable via the dashboard's Reference Tables tab, and keeping
    # unused raw fields alongside the derived ones they were split from would
    # let someone edit one without the other, silently drifting the two out
    # of sync.
    ensure_table(
        AGENT_ADDRESSES_TABLE,
        key_schema=[{"AttributeName": "Code", "KeyType": "HASH"}],
        attribute_definitions=[{"AttributeName": "Code", "AttributeType": "S"}],
    )
    table = dynamodb.Table(AGENT_ADDRESSES_TABLE)
    tps = TPS(INVCODE_TPS_PATH, encoding=TPS_ENCODING, cached=True, check=True, current_tablename="UNNAMED")
    count = 0
    with table.batch_writer(overwrite_by_pkeys=["Code"]) as batch:
        for r in tps:
            code = normalize_key_text(clean_tps_field(r.get("b'INV:CODE'")))
            if not code or code in INDIA_DUPLICATE_CODES_TO_SKIP:
                continue
            address1 = clean_tps_field(r.get("b'INV:INDIANADDRESS'"))
            address2 = clean_tps_field(r.get("b'INV:INDIANADDRESS2'"))
            address3 = clean_tps_field(r.get("b'INV:INDIANADDRESS3'"))
            fssai = clean_tps_field(r.get("b'INV:INDIANFSSAINR'"))
            importer_name, address_lines = split_name_and_address(address1, address2, address3)
            item = {
                "Code": code,
                "ImporterName": importer_name,
                "AddressLines": address_lines,
            }
            if fssai:
                item["FSSAI"] = fssai
            batch.put_item(Item=item)
            count += 1
    print(f"{AGENT_ADDRESSES_TABLE}: loaded {count} India rows (from InvCode.TPS).")


def sync_general_agents():
    # Non-India agent/importer codes (Malaysia's 17A and onward) -- same
    # table, same Code/ImporterName/AddressLines shape as the India loader
    # above, just a different source table (Customers.TPS: CUS:CODE,
    # CUS:CUSTOMER, CUS:ADDRESS1-4, no FSSAI). "Customer" is a short internal
    # nickname (e.g. "CHOP TONG"), not what's printed on a real label --
    # Address1 carries the full legal company name instead (confirmed
    # against a real 17A photo: agent "5K"'s Address1 "T&T FRESH SDN BHD" is
    # exactly what the label prints), so Address1 feeds ImporterName via the
    # same split_name_and_address() used for India, not CUS:CUSTOMER.
    #
    # Runs after sync_agent_addresses() (same order as before this read from
    # TPS directly) so Customers.TPS wins on the one known Code overlap
    # between the two sources (code "LM" / FRESHPRO INTERNATIONAL, present
    # in both as the same company -- not a conflict like the historical "XX"
    # case below, just keeping today's precedence unchanged on principle).
    ensure_table(
        AGENT_ADDRESSES_TABLE,
        key_schema=[{"AttributeName": "Code", "KeyType": "HASH"}],
        attribute_definitions=[{"AttributeName": "Code", "AttributeType": "S"}],
    )
    table = dynamodb.Table(AGENT_ADDRESSES_TABLE)
    tps = TPS(CUSTOMERS_TPS_PATH, encoding=TPS_ENCODING, cached=True, check=True, current_tablename="UNNAMED")
    count = 0
    with table.batch_writer(overwrite_by_pkeys=["Code"]) as batch:
        for r in tps:
            code = normalize_key_text(clean_tps_field(r.get("b'CUS:CODE'")))
            if not code:
                continue
            address1 = clean_tps_field(r.get("b'CUS:ADDRESS1'"))
            address2 = clean_tps_field(r.get("b'CUS:ADDRESS2'"))
            address3 = clean_tps_field(r.get("b'CUS:ADDRESS3'"))
            address4 = clean_tps_field(r.get("b'CUS:ADDRESS4'"))
            importer_name, address_lines = split_name_and_address(address1, address2, address3, address4)
            batch.put_item(Item={
                "Code": code,
                "ImporterName": importer_name,
                "AddressLines": address_lines,
            })
            count += 1
    print(f"{AGENT_ADDRESSES_TABLE}: loaded {count} general-agent rows (from Customers.TPS).")


def main():
    sync_variety()
    sync_variety_group()
    sync_ian_numbers()
    sync_rewe_combinations()
    sync_agent_addresses()
    sync_general_agents()


if __name__ == "__main__":
    main()
