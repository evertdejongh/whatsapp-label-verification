#!/usr/bin/env bash
# Creates an IAM user for local AWS CLI/boto3 development on this project,
# with the AWS-managed PowerUserAccess policy (full access to all services
# except IAM/account/org management -- so a leaked local key can't create
# new users, escalate privileges, or touch billing/account settings).
#
# Run this once in AWS CloudShell (region us-east-2). Safe to re-run.

set -euo pipefail

ACCOUNT_ID="677513501349"
REGION="us-east-2"
USER_NAME="evertj-local-dev"
POLICY_ARN="arn:aws:iam::aws:policy/PowerUserAccess"

echo "Creating IAM user ${USER_NAME}..."
if aws iam get-user --user-name "${USER_NAME}" >/dev/null 2>&1; then
  echo "User already exists."
else
  aws iam create-user --user-name "${USER_NAME}" >/dev/null
fi

echo "Attaching PowerUserAccess policy..."
aws iam attach-user-policy \
  --user-name "${USER_NAME}" \
  --policy-arn "${POLICY_ARN}"

echo
echo "Creating access key (this is only shown once -- copy it now)..."
aws iam create-access-key --user-name "${USER_NAME}"

echo
echo "Done. Copy the AccessKeyId/SecretAccessKey above, then on your"
echo "laptop run:"
echo "  aws configure --profile evertj-dev"
echo "and paste them in when prompted (region: ${REGION}, output: json)."
