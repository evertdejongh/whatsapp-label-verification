#!/usr/bin/env python3
"""
Syncs the Dole Table Grapes Pack Specifications catalog into AWS:
  1. Creates the whatsapp-spec-catalog DynamoDB table if it doesn't exist.
  2. Downloads each spec sheet PDF from its dl.d6.co.za source URL.
  3. Uploads each PDF to S3 at specs/{SPEC_CODE}_specsheet.pdf.
  4. Writes/overwrites a DynamoDB item per spec code (spec_code, description, pdf_url, pdf_s3_key).

Safe to re-run any time you get an updated spreadsheet from Dole -- just
update SPEC_ROWS below with the new/changed rows and re-run. Existing S3
objects and DynamoDB items are simply overwritten.

Run this in AWS CloudShell (region us-east-2), not on a local machine --
it needs both real internet egress and working AWS credentials.
"""

import json
import time
import urllib.request

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-2"
BUCKET_NAME = "dole-pallet-specs-2026-677513501349-us-east-2-an"
TABLE_NAME = "whatsapp-spec-catalog"

SPEC_ROWS = [
  {
    "spec_code": "00",
    "description": "AIRFREIGHT GRAPES | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6955012489157152455312/00_AIRFREIGHTGRAPES_2026_V1-0.pdf"
  },
  {
    "spec_code": "10A",
    "description": "BAMA | 10x500g Clamshell Punnets Bendit Label | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_695f78caa99ab110786299/10a_BAMA_10x500gclamshellpunnetsBenditlabel_2026_V1-1.pdf"
  },
  {
    "spec_code": "10B",
    "description": "BAMA | 10x500g Clamshell Punnets Cevita Label | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6968f511ec5a4000811579/10b_BAMA_10x500gclamshellpunnetsCevitalabel_2026_V1-1.pdf"
  },
  {
    "spec_code": "10C",
    "description": "BAMA | 4.5kg Dole Sliderbags | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6953e879117c6254423833/10c_BAMA_4.5kgDoleSliderbags_2026_V1-0.pdf"
  },
  {
    "spec_code": "12A",
    "description": "COSTCO USA | 6x1.36kg Clamshell Punnet Fresh Label And Sleeve | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_694285a2d26ff919214754/12a_COSTCOUSA_6x1.36kgclamshellpunnetFreshlabelandsleeve_2026_V1-0.pdf"
  },
  {
    "spec_code": "13A",
    "description": "REWE | 10x500g Clamshell Punnet Markise Label | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_69397995233b1838455639/13a_REWE_10x500gclamshellpunnetMarkiselabel_2026_V1-1.pdf"
  },
  {
    "spec_code": "13B",
    "description": "REWE | 4.5kg Beste Wahl Paper Bags | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_69397997a639c997948572/13b_REWE_4.5kgBesteWahlpaperbags_2026_V1-1.pdf"
  },
  {
    "spec_code": "13C",
    "description": "REWE | 10x500g Clamshell Punnet Dole Label | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6939799a274c9713695296/13c_REWE_10x500gclamshellpunnetDolelabel_2026_V1-1.pdf"
  },
  {
    "spec_code": "13D",
    "description": "REWE | 10x500g Clamshell Punnet Beste Wahl Mix Label | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6939799ca6388443785649/13d_REWE_10x500gclamshellpunnetBesteWahlMixlabel_2026_V1-1.pdf"
  },
  {
    "spec_code": "14A",
    "description": "ISRAEL | 10x500g Clamshell Punnet Generic Label | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_69660e32e2079144671835/14a_ISRAEL_10x500gclamshellpunnetGenericlabel_2026_V1-1.pdf"
  },
  {
    "spec_code": "14B",
    "description": "ISRAEL | 10x500g Clamshell Punnet Dole Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6932db1171d31249189847/14b_ISRAEL_10x500gclamshellpunnetDolelabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "14C",
    "description": "ISRAEL | 4.5kg Sliderbags | 2026 | V1.2",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6965f6785146f065347149/14c_ISRAEL_4.5kgsliderbags_2026_V1-2.pdf"
  },
  {
    "spec_code": "15A",
    "description": "RUSSIA | 4.5kg | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_695d04df44e2c636586780/15a_RUSSIA_4.5kg_2026_V1-0.pdf"
  },
  {
    "spec_code": "16A",
    "description": "DOLE NORDIC | 10x500g Clamshell Punnets COOP Plastic Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6936cedaf0c6b852372069/16a_DOLENORDIC_10x500gclamshellpunnetsCOOPplasticlabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "16B",
    "description": "DOLE NORDIC | 10x500g Clamshell Punnets Salling Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6953cccbb8fae732865929/16b_DOLENORDIC_10x500gclamshellpunnetsSallinglabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "16C",
    "description": "DOLE NORDIC | 6x1.36 Kg Clamshell Punnets Dole Nordic Labels | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6938135f9ca9a418160880/16c_DOLENORDIC_6x1.36kgclamshellpunnetsDoleNordiclabels_2026_V1-0.pdf"
  },
  {
    "spec_code": "16D",
    "description": "DOLE NORDIC | 10x500g Clamshell Punnets COOP Mer Smak S35 Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6957bae8ab2c9900790269/16d_DOLENORDIC_10x500gclamshellpunnetsCOOPMerSmakS35label_2026_V1-0.pdf"
  },
  {
    "spec_code": "16E",
    "description": "DOLE NORDIC | 6x750g Clamshell Punnets Salling S35 Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6953c64e57c44652950476/16e_DOLENORDIC_6x750gclamshellpunnetsSallingS35label_2026_V1-0.pdf"
  },
  {
    "spec_code": "16F",
    "description": "DOLE NORDIC | 10x500g Clamshell Punnets Gestus Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6932aa00b200a978515498/16f_DOLENORDIC_10x500gclamshellpunnetsGestuslabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "16G",
    "description": "DOLE NORDIC | 9x250g Clamshell Punnets Salling Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6957baeaed804922279180/16g_DOLENORDIC_9x250gclamshellpunnetsSallinglabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "17A",
    "description": "MALAYSIA | 4.5kg Sliderbags, 10x500g Clamshell Dole Punnet Labels | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6968f83938a11630806898/17a_MALAYSIA_4.5kgsliderbags10x500gclamshellDolepunnetlabels_2026_V1-1.pdf"
  },
  {
    "spec_code": "17B",
    "description": "SAUDI ARABIA | 4.5kg | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_694962d56b668043434787/17b_SAUDIARABIA_4.5kg_2026_V1-0.pdf"
  },
  {
    "spec_code": "17C",
    "description": "INDIA | 4.5kg | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6979c48a18ceb142652017/17c_INDIA_4.5kg_2026_V1-0.pdf"
  },
  {
    "spec_code": "18A",
    "description": "DOLE ITALY | 6x800g Clamshell Punnets No Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6957cc06afb8e533335236/18a_DOLEITALY_6x800gclamshellpunnetsNolabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "1A",
    "description": "4.5kg Dole Primore Cartons | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_692d9eb678875567620634/1a_4.5kgDolePrimorecartons_2026_V1-0.pdf"
  },
  {
    "spec_code": "1B",
    "description": "10x500g Clamshell Heatseal punnets Dole Or Generic Label, No Label | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_693ab5ff919b3773734209/1b_10x500gclamshellpunnetsDoleorgenericlabelnolabel_2026_V1-1.pdf"
  },
  {
    "spec_code": "1C",
    "description": "DOLE FRANCE | 10x500g Clamshell Punnets Generic Label | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_693961dad355f171259998/1c_DOLEFRANCE_10x500gclamshellpunnetsgenericlabel_2026_V1-1.pdf"
  },
  {
    "spec_code": "20B",
    "description": "CANADA GAMBLES | 10x500g Clamshell Punnets Dole EN FR Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6932b034634a2852974297/20b_CANADAGAMBLES_10x500gclamshellpunnetsDoleENFRlabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "3A",
    "description": "AMFRESH COSTCO ES | 6x1.5kg Clamshell Punnets No Label | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_69806f06896c0799427906/3a_AMFRESH_COSTCOES_6x1.5kgclamshellpunnetsnolabel_2026_V1-1.pdf"
  },
  {
    "spec_code": "4A",
    "description": "DOLE IE | TESCO DUNNES 11x500g Heatseal Punnets | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_691b12df08f6c672313313/4a_DOLEIETESCODUNNES_11x500gheatsealpunnets_2026_V1-0.pdf"
  },
  {
    "spec_code": "5A",
    "description": "ALDI NORD NL | 10x500g Clamshell Punnets ALDI Druiven Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_69305aa658ca8535745784/5a_ALDINORDNL_10x500gclamshellpunnetsALDIDruivenlabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "5B",
    "description": "ALDI NORD DE | 10x500g Clamshell Punnets ALDI German Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_69305aa8ab601325704652/5b_ALDINORDDE_10x500gclamshellpunnetsALDIGermanlabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "5C",
    "description": "ALDI NORD | 6x650g ALDI Ziplock Bags | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_69305b049c282070322700/5c_ALDINORD_6x650gALDIziplockbags_2026_V1-0.pdf"
  },
  {
    "spec_code": "5D",
    "description": "ALDI NORD | 10x500g Clamshell Punnets ALDI Combo Underside Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_69305b06ece14697096806/5d_ALDINORD_10x500gclamshellpunnetsALDIcomboundersidelabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "6A",
    "description": "ALDI UK | 11x500g Heatseal Punnets | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_696e45e75fd83318972315/6a_ALDIUK_11x500gheatsealpunnets_2026_V1-1.pdf"
  },
  {
    "spec_code": "6B",
    "description": "LIDL UK | 11x500g Heatseal Punnets | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6936cb6a20ce5061185948/6b_LIDLUK_11x500gheatsealpunnets_2026_V1-0.pdf"
  },
  {
    "spec_code": "6C",
    "description": "ALDI & LIDL UK | 11x400g SGS Heatseal Punnets | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6932b2aba4a12961496962/6c_ALDILIDLUK_11x400gSGSheatsealpunnets_2026_V1-0.pdf"
  },
  {
    "spec_code": "7A",
    "description": "BIEDRONKA | 4.5kg Paper Bags | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6942854040b87639657917/7a_BIEDRONKA_4.5kgpaperbags_2026_V1-1.pdf"
  },
  {
    "spec_code": "8A",
    "description": "EDEKA | 10x500g Clamshell Punnets G&G Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6943d438b498e228806696/8a_EDEKA_10x500gclamshellpunnetsGGlabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "8B",
    "description": "EDEKA | 4.5kg Herzstucke Paper Bags | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_692d50a0bf161352182977/8b_EDEKA_4.5kgHerzstuckepaperbags_2026_V1-0.pdf"
  },
  {
    "spec_code": "8C",
    "description": "EDEKA NETTO | 4.5kg Paperbags CSS TIM ALI | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_692d50a31e342765635131/8c_EDEKANETTO_4.5kgpaperbagsCSSTIMALI_2026_V1-0.pdf"
  },
  {
    "spec_code": "8D",
    "description": "EDEKA NETTO | 10x500g Clamshell Punnets Netto Markttag Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_692d50a57938c243232880/8d_EDEKANETTO_10x500gclamshellpunnetsNettoMarkttaglabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "8E",
    "description": "EDEKA | 10x500g Clamshell Punnets Trauben Mix Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_692d50a7df0e7132396562/8e_EDEKA_10x500gclamshellpunnetsTraubenMixlabel_2026_V1-0.pdf"
  },
  {
    "spec_code": "8F",
    "description": "EDEKA | 10x400g Heat Seal Punnets S35 | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_695d04b0da6d1903237096/8f_EDEKA_10x400gheatsealpunnetsS35_2026_V1-0.pdf"
  },
  {
    "spec_code": "9A",
    "description": "LIDL EU | 4.5kg Generic Paper Bag | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_69269c241c41b532375421/9a_LIDLEU_4.5kgGenericpaperbag_2026_V1-0.pdf"
  },
  {
    "spec_code": "9B",
    "description": "LIDL EU | 10x500g Clamshell Punnets LIDL Label | 2026 | V1.1",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_6942846aec339119321778/9b_LIDLEU_10x500gclamshellpunnetsLIDLlabel_2026_V1-1.pdf"
  },
  {
    "spec_code": "9C",
    "description": "LIDL EU | 10x500g Clamshell Punnets Dole Label | 2026 | V1.0",
    "pdf_url": "https://dl.d6.co.za/attachments/cf_69305bae7965d956236759/9c_LIDLEU_10x500gclamshellpunnetsDolelabel_2026_V1-0.pdf"
  }
]

s3 = boto3.client("s3", region_name=REGION)
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
        KeySchema=[{"AttributeName": "spec_code", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "spec_code", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)
    print(f"Table {TABLE_NAME} created.")


def download_pdf(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def main():
    ensure_table()
    table = dynamodb.Table(TABLE_NAME)

    ok, failed = [], []
    for row in SPEC_ROWS:
        spec_code = row["spec_code"]
        description = row["description"]
        pdf_url = row["pdf_url"]
        s3_key = f"specs/{spec_code}_specsheet.pdf"

        try:
            print(f"[{spec_code}] downloading...")
            pdf_bytes = download_pdf(pdf_url)

            print(f"[{spec_code}] uploading to s3://{BUCKET_NAME}/{s3_key} ({len(pdf_bytes)} bytes)")
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=s3_key,
                Body=pdf_bytes,
                ContentType="application/pdf",
            )

            table.put_item(
                Item={
                    "spec_code": spec_code,
                    "description": description,
                    "pdf_url": pdf_url,
                    "pdf_s3_key": s3_key,
                }
            )
            print(f"[{spec_code}] done.")
            ok.append(spec_code)
        except Exception as e:
            print(f"[{spec_code}] FAILED: {e}")
            failed.append((spec_code, str(e)))

        time.sleep(0.2)  # light throttle against the source host

    print()
    print(f"Synced {len(ok)}/{len(SPEC_ROWS)} spec sheets.")
    if failed:
        print("Failed:")
        for code, err in failed:
            print(f"  {code}: {err}")


if __name__ == "__main__":
    main()
