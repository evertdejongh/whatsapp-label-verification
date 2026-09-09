#!/usr/bin/env bash
# Grants the whatsapp-audit-dashboard Lambda's execution role full CRUD
# access to the DynamoDB reference tables browsable via the dashboard's
# "Reference Tables" tab, plus S3 list/get/put/delete on the specs/ prefix
# for the "Spec Files" tab (image/config browsing, inline JSON editing,
# upload, delete). Unlike the webhook's read-only grant
# (attach_webhook_lookup_permissions.sh), this is a genuinely destructive
# set of permissions -- Scan/PutItem/DeleteItem, not just GetItem/Query --
# since this is an admin tool, not the validation pipeline itself.
#
# Looks up the role automatically from the live function config. Safe to
# re-run -- put-role-policy overwrites the named inline policy each time.

set -euo pipefail

ACCOUNT_ID="677513501349"
REGION="us-east-2"
FUNCTION_NAME="whatsapp-audit-dashboard"
POLICY_NAME="whatsapp-dashboard-admin-access"
BUCKET_NAME="dole-pallet-specs-2026-677513501349-us-east-2-an"
TABLES=(
  "whatsapp-puc"
  "whatsapp-variety"
  "whatsapp-variety-group"
  "whatsapp-ian-numbers"
  "whatsapp-commodity"
  "whatsapp-rewe-combinations"
  "whatsapp-india-addresses"
)

ROLE_ARN=$(aws lambda get-function-configuration \
  --function-name "${FUNCTION_NAME}" \
  --region "${REGION}" \
  --query "Role" --output text)
ROLE_NAME="${ROLE_ARN##*/}"

echo "Found execution role: ${ROLE_NAME}"

TABLE_RESOURCES_JSON=""
for t in "${TABLES[@]}"; do
  if [ -n "${TABLE_RESOURCES_JSON}" ]; then
    TABLE_RESOURCES_JSON="${TABLE_RESOURCES_JSON},"
  fi
  TABLE_RESOURCES_JSON="${TABLE_RESOURCES_JSON}\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${t}\""
done

POLICY_FILE="${TMPDIR:-.}/dashboard-admin-policy.json"
cat > "${POLICY_FILE}" <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReferenceTablesAdminAccess",
      "Effect": "Allow",
      "Action": [
        "dynamodb:Scan",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:DeleteItem"
      ],
      "Resource": [${TABLE_RESOURCES_JSON}]
    },
    {
      "Sid": "SpecFilesListAccess",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::${BUCKET_NAME}",
      "Condition": {
        "StringLike": { "s3:prefix": "specs/*" }
      }
    },
    {
      "Sid": "SpecFilesObjectAccess",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::${BUCKET_NAME}/specs/*"
    }
  ]
}
POLICY

echo "Attaching inline policy ${POLICY_NAME} to ${ROLE_NAME}..."
aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${POLICY_NAME}" \
  --policy-document "file://${POLICY_FILE}"

echo "Done."
