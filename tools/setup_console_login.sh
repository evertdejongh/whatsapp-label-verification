#!/usr/bin/env bash
# Adds a console (password) login to the existing evertj-local-dev IAM user,
# so day-to-day AWS Console browsing (Lambda, DynamoDB, CloudWatch, S3, etc.)
# never needs the root account. Same PowerUserAccess permission scope as its
# existing CLI access key -- this just adds a second way to authenticate as
# that same user.
#
# Run this once in AWS CloudShell (region us-east-2). Safe to re-run: if a
# login profile already exists, it resets the password instead of failing.

set -euo pipefail

ACCOUNT_ID="677513501349"
USER_NAME="evertj-local-dev"

TEMP_PASSWORD="$(openssl rand -base64 18)Aa1!"

# PowerUserAccess deliberately excludes IAM actions, including the ability
# to change your own console password -- attach the narrow AWS-managed
# policy that grants exactly that self-service permission.
echo "Attaching IAMUserChangePassword policy..."
aws iam attach-user-policy \
  --user-name "${USER_NAME}" \
  --policy-arn "arn:aws:iam::aws:policy/IAMUserChangePassword"

echo "Setting up console login for ${USER_NAME}..."
if aws iam get-login-profile --user-name "${USER_NAME}" >/dev/null 2>&1; then
  echo "Login profile already exists, resetting password..."
  aws iam update-login-profile \
    --user-name "${USER_NAME}" \
    --password "${TEMP_PASSWORD}" \
    --password-reset-required
else
  aws iam create-login-profile \
    --user-name "${USER_NAME}" \
    --password "${TEMP_PASSWORD}" \
    --password-reset-required
fi

echo
echo "Done. Sign in at:"
echo "  https://${ACCOUNT_ID}.signin.aws.amazon.com/console"
echo
echo "  Account ID:  ${ACCOUNT_ID}"
echo "  IAM username: ${USER_NAME}"
echo "  Temporary password: ${TEMP_PASSWORD}"
echo
echo "You'll be forced to set a new password on first sign-in (copy the"
echo "temporary one above now -- it's only shown here once). After signing"
echo "in, set up MFA under your account menu > Security credentials for an"
echo "extra layer of protection."
