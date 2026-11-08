#!/usr/bin/env bash

# =========================
# CONFIG
# =========================
SERVER="localhost"
PORT="8000"
BASE="http://$SERVER:$PORT"

USERNAME="temp_student"
PASSWORD="temp_student"

echo "======================================"
echo " File Browser Deep Diagnosis v2"
echo " Target: $BASE"
echo "======================================"
echo ""

# =========================
# 1. LOGIN (JWT RAW)
# =========================
echo "[1] Login test"

LOGIN=$(curl -s -X POST "$BASE/api/login" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$USERNAME\",\"password\":\"$PASSWORD\"}")

echo "Raw login response:"
echo "$LOGIN"
echo ""

# Extract token safely
TOKEN=$(echo "$LOGIN" | grep -Eo 'eyJ[a-zA-Z0-9._-]+' | head -n 1)

echo "Extracted token:"
echo "[$TOKEN]"
echo ""

if [ -z "$TOKEN" ]; then
  echo "❌ ERROR: No token extracted"
  exit 1
fi

# =========================
# 2. DETECT WORKING API PATH
# =========================
echo "[2] Detecting API base path"

API1="$BASE/api/resources?path=/"
API2="$BASE/filebrowser/api/resources?path=/"
API3="$BASE/api/v1/resources?path=/"

echo "Testing /api/resources"
C1=$(curl -s -o /dev/null -w "%{http_code}" "$API1")

echo "Testing /filebrowser/api/resources"
C2=$(curl -s -o /dev/null -w "%{http_code}" "$API2")

echo "Testing /api/v1/resources"
C3=$(curl -s -o /dev/null -w "%{http_code}" "$API3")

echo "Results:"
echo "  /api         -> $C1"
echo "  /filebrowser -> $C2"
echo "  /api/v1      -> $C3"
echo ""

# Choose best endpoint
if [ "$C2" = "200" ]; then
  API="$API2"
  echo "✔ Using /filebrowser/api (detected)"
elif [ "$C3" = "200" ]; then
  API="$API3"
  echo "✔ Using /api/v1 (detected)"
else
  API="$API1"
  echo "✔ Defaulting to /api"
fi

echo ""

# =========================
# 3. TEST AUTH METHODS (correct one = X-Auth)
# =========================
echo "[3] Testing X-Auth (correct method)"

AUTH_HEADER="X-Auth: $TOKEN"

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$API" \
  -H "$AUTH_HEADER")

echo "X-Auth result: HTTP $HTTP_CODE"
echo ""

# =========================
# 4. TEST WRONG METHODS (for diagnosis clarity)
# =========================
echo "[4] Testing incorrect auth methods"

curl -s -o /dev/null -w "Bearer: HTTP %{http_code}\n" "$API" \
  -H "Authorization: Bearer $TOKEN"

curl -s -o /dev/null -w "Cookie: HTTP %{http_code}\n" "$API"

echo ""

# =========================
# 5. VALID DATA FETCH
# =========================
echo "[5] Fetching directory listing (folders only)"

BODY=$(curl -s "$API" -H "$AUTH_HEADER")

if echo "$BODY" | grep -q '"name"'; then
  echo "✔ Valid API response (JSON-like structure detected)"
else
  echo "⚠️ Unexpected format (not necessarily HTML)"
fi

echo ""

# Optional: list folders
echo "$BODY" | grep -o '"name":"[^"]*"' | head

echo ""

# =========================
# 6. SUMMARY
# =========================
echo "======================================"
echo " DIAGNOSIS COMPLETE"
echo "======================================"
echo ""
echo "Correct API: $API"
echo "Auth method: X-Auth"
echo ""
echo "Expected working command:"
echo ""
echo "curl \"$API\" \\"
echo "  -H \"X-Auth: <TOKEN>\""
echo ""