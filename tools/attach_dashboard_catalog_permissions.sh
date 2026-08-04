#!/usr/bin/env bash
# Grants the whatsapp-audit-dashboard Lambda's execution role the
# permissions its new Spec Catalog tab needs: read/write on the
# whatsapp-spec-catalog DynamoDB table, and write/delete on cached PDF
# objects in S3. Looks up the role automatically from the live function
# config, so you don't need to know its exact (auto-generated) name.
#
# Run this once in AWS CloudShell (region us-east-2). Safe to re-run --
# put-role-policy overwrites the named inline policy each time.

set -euo pipefail

ACCOUNT_ID="677513501349"
REGION="us-east-2"
FUNCTION_NAME="whatsapp-audit-dashboard"
BUCKET_NAME="dole-pallet-specs-2026-677513501349-us-east-2-an"
TABLE_NAME="whatsapp-spec-catalog"
POLICY_NAME="whatsapp-spec-catalog-dashboard-access"

ROLE_ARN=$(aws lambda get-function-configuration \
  --function-name "${FUNCTION_NAME}" \
  --region "${REGION}" \
  --query "Role" --output text)
ROLE_NAME="${ROLE_ARN##*/}"

echo "Found execution role: ${ROLE_NAME}"

cat > /tmp/spec-catalog-dashboard-policy.json <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SpecCatalogTableAccess",
      "Effect": "Allow",
      "Action": [
        "dynamodb:Scan",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:DeleteItem"
      ],
      "Resource": "arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${TABLE_NAME}"
    },
    {
      "Sid": "SpecSheetPdfWrite",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::${BUCKET_NAME}/specs/*_specsheet.pdf"
    }
  ]
}
POLICY

echo "Attaching inline policy ${POLICY_NAME} to ${ROLE_NAME}..."
aws iam put-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-name "${POLICY_NAME}" \
  --policy-document file:///tmp/spec-catalog-dashboard-policy.json

echo "Done."
