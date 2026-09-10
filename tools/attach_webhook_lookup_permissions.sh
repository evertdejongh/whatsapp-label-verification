#!/usr/bin/env bash
# Grants the whatsapp-label-verifier Lambda's execution role read-only
# access to the new reference-data tables used for cross-table label
# validation (PUC -> GGN, Variety -> Variety Group/Commodity, etc). The
# webhook does point lookups (GetItem) plus a partition-key Query on the
# Variety table (VarietyName alone can return more than one commodity's
# row, disambiguated by the caller) -- no Scan/Put/Delete, unlike the
# dashboard's catalog-management policy. Looks up the role automatically
# from the live function config.
#
# Safe to re-run -- put-role-policy overwrites the named inline policy
# each time.

set -euo pipefail

ACCOUNT_ID="677513501349"
REGION="us-east-2"
FUNCTION_NAME="whatsapp-label-verifier"
POLICY_NAME="whatsapp-reference-tables-read-access"
TABLES=(
  "whatsapp-variety"
  "whatsapp-variety-group"
  "whatsapp-ian-numbers"
  "whatsapp-puc"
  "whatsapp-commodity"
  "whatsapp-rewe-combinations"
  "whatsapp-agent-addresses"
)

ROLE_ARN=$(aws lambda get-function-configuration \
  --function-name "${FUNCTION_NAME}" \
  --region "${REGION}" \
  --query "Role" --output text)
ROLE_NAME="${ROLE_ARN##*/}"

echo "Found execution role: ${ROLE_NAME}"

RESOURCES_JSON=""
for t in "${TABLES[@]}"; do
  if [ -n "${RESOURCES_JSON}" ]; then
    RESOURCES_JSON="${RESOURCES_JSON},"
  fi
  RESOURCES_JSON="${RESOURCES_JSON}\"arn:aws:dynamodb:${REGION}:${ACCOUNT_ID}:table/${t}\""
done

POLICY_FILE="${TMPDIR:-.}/webhook-reference-tables-policy.json"
cat > "${POLICY_FILE}" <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReferenceTablesReadAccess",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:Query"
      ],
      "Resource": [${RESOURCES_JSON}]
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
