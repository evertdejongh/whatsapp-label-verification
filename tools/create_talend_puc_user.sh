#!/usr/bin/env bash
# Creates a dedicated IAM user for Talend to read/write ONLY the whatsapp-puc
# DynamoDB table -- a separate, narrowly-scoped credential rather than
# reusing a personal AWS profile for a third-party tool. Run this in AWS
# CloudShell (or anywhere with IAM admin rights); it prints a real,
# long-lived secret access key at the end -- copy it straight into Talend's
# DynamoDB connection settings and don't paste it anywhere else (chat,
# tickets, etc).
#
# Safe to re-run for the policy/user creation steps (idempotent), but each
# run creates a NEW access key -- if you re-run this to rotate the key,
# deactivate/delete the old one afterward (see the aws iam commands at the
# bottom of this file for how).

set -euo pipefail

REGION="us-east-2"
ACCOUNT_ID="677513501349"
USER_NAME="talend-puc-sync"
POLICY_NAME="whatsapp-puc-table-access"
TABLE_ARN="arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/whatsapp-puc"

# Create the user if it doesn't already exist.
if aws iam get-user --user-name "${USER_NAME}" >/dev/null 2>&1; then
  echo "User ${USER_NAME} already exists."
else
  echo "Creating IAM user ${USER_NAME}..."
  aws iam create-user --user-name "${USER_NAME}"
fi

POLICY_FILE="${TMPDIR:-.}/talend-puc-policy.json"
cat > "${POLICY_FILE}" <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WhatsappPucTableAccess",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Scan",
        "dynamodb:Query",
        "dynamodb:BatchGetItem",
        "dynamodb:BatchWriteItem"
      ],
      "Resource": "${TABLE_ARN}"
    }
  ]
}
POLICY

echo "Attaching inline policy ${POLICY_NAME} (scoped to whatsapp-puc only)..."
aws iam put-user-policy \
  --user-name "${USER_NAME}" \
  --policy-name "${POLICY_NAME}" \
  --policy-document "file://${POLICY_FILE}"

echo ""
echo "Creating a new access key for ${USER_NAME}..."
echo "=== COPY THESE INTO TALEND'S DYNAMODB CONNECTION SETTINGS NOW ==="
aws iam create-access-key --user-name "${USER_NAME}" \
  --query "AccessKey.[AccessKeyId,SecretAccessKey]" --output text
echo "=================================================================="
echo "This secret access key will not be shown again -- if you lose it,"
echo "re-run this script's create-access-key step to issue a new one, and"
echo "delete the old one with:"
echo "  aws iam list-access-keys --user-name ${USER_NAME}"
echo "  aws iam delete-access-key --user-name ${USER_NAME} --access-key-id <old-key-id>"
echo ""
echo "Done. Region for the Talend connection: ${REGION}."
