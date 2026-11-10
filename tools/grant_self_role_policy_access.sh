#!/usr/bin/env bash
# ONE-TIME, run under an admin/root AWS session (NOT evertj-local-dev --
# PowerUserAccess deliberately excludes IAM actions, which is exactly what
# this script needs to attach). Grants the evertj-local-dev IAM user just
# enough IAM access to self-serve attach_webhook_lookup_permissions.sh /
# attach_dashboard_admin_permissions.sh going forward, scoped to exactly
# the two Lambda execution roles those scripts touch -- it can still never
# touch any other role, create users, or do anything else IAM-related.
#
# This has come up repeatedly (REWE, 17A, the india-addresses rename, and
# now EdekaGTINs) -- every new reference table needed a CloudShell/root
# run to attach the read/CRUD policy. This removes that recurring need.
#
# Safe to re-run -- put-user-policy overwrites the named inline policy.

set -euo pipefail

ACCOUNT_ID="677513501349"
IAM_USER="evertj-local-dev"
POLICY_NAME="self-serve-whatsapp-role-policies"
WEBHOOK_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/service-role/whatsapp-label-verifier-role-t4p7zlz8"
DASHBOARD_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/service-role/whatsapp-audit-dashboard-role-a4zrsf78"

POLICY_FILE="${TMPDIR:-.}/self-serve-whatsapp-role-policies.json"
cat > "${POLICY_FILE}" <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SelfServeWhatsAppRolePolicies",
      "Effect": "Allow",
      "Action": [
        "iam:PutRolePolicy",
        "iam:GetRolePolicy",
        "iam:ListRolePolicies"
      ],
      "Resource": [
        "${WEBHOOK_ROLE_ARN}",
        "${DASHBOARD_ROLE_ARN}"
      ]
    }
  ]
}
POLICY

echo "Attaching inline policy ${POLICY_NAME} to IAM user ${IAM_USER}..."
aws iam put-user-policy \
  --user-name "${IAM_USER}" \
  --policy-name "${POLICY_NAME}" \
  --policy-document "file://${POLICY_FILE}"

echo "Done. evertj-local-dev can now self-serve PutRolePolicy on just these two roles."
