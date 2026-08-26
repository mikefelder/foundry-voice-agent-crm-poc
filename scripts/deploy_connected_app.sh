#!/usr/bin/env bash
# Create the Connected App for the JWT bearer flow by deploying metadata.
#
# Newer orgs removed "New Connected App" from App Manager, leaving only Lightning
# and External Client apps in the UI. The ConnectedApp metadata type still works,
# and unlike clicking through Setup it is reproducible against any org.
#
# The app is rendered from .secrets/server.crt at deploy time so no keypair-specific
# file is committed.
set -euo pipefail

ORG="${1:-devorg}"
CERT="${CERT:-.secrets/server.crt}"
APP_LABEL="${APP_LABEL:-CRM Sales Companion}"
APP_NAME="${APP_NAME:-CRM_Sales_Companion}"
PROFILE="${PROFILE:-System Administrator}"

[[ -f "$CERT" ]] || { echo "Missing $CERT - run scripts/new_jwt_cert.sh first" >&2; exit 1; }

CONTACT=$(sf org display --target-org "$ORG" --json | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['username'])")

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/connectedApps"

# The metadata field wants the certificate body only, no PEM header or line breaks.
CERT_BODY=$(grep -v CERTIFICATE "$CERT" | tr -d '\n')

cat > "$STAGE/connectedApps/${APP_NAME}.connectedApp-meta.xml" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<ConnectedApp xmlns="http://soap.sforce.com/2006/04/metadata">
    <contactEmail>${CONTACT}</contactEmail>
    <label>${APP_LABEL}</label>
    <oauthConfig>
        <callbackUrl>http://localhost:1717/OauthRedirect</callbackUrl>
        <certificate>${CERT_BODY}</certificate>
        <isAdminApproved>true</isAdminApproved>
        <scopes>Api</scopes>
        <scopes>RefreshToken</scopes>
    </oauthConfig>
    <profileName>${PROFILE}</profileName>
</ConnectedApp>
XML

cat > "$STAGE/package.xml" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<Package xmlns="http://soap.sforce.com/2006/04/metadata">
    <types>
        <members>${APP_NAME}</members>
        <name>ConnectedApp</name>
    </types>
    <version>62.0</version>
</Package>
XML

echo "Deploying ${APP_LABEL} to ${ORG}..."
sf project deploy start --target-org "$ORG" --metadata-dir "$STAGE" --wait 10

echo
echo "Consumer key:"
sf org list metadata --target-org "$ORG" --metadata-type ConnectedApp >/dev/null 2>&1 || true
echo "  Setup > App Manager > ${APP_LABEL} > View > Manage Consumer Details"
echo
echo "OAuth changes can take a few minutes to propagate before JWT succeeds."
