#!/bin/bash
# Install the module into a fresh database of the given Odoo version and run its tests.
#   ./run_tests.sh 17   (docker: odoo:<v> and the aipbx-odoo-pg postgres on network aipbx-odoo)
set -euo pipefail
v="${1:?usage: run_tests.sh 17|18|19}"
here="$(cd "$(dirname "$0")" && pwd)"
db="aipbx_test_${v}_$(date +%s)"
addons="$here"
if [ "$v" != 17 ]; then python3 "$here/make_version.py" "$v" >/dev/null; addons="$here/build/$v.0"; fi
demo="--without-demo=all"
[ "$v" -ge 19 ] && demo=""
cleanup() {
  docker exec aipbx-odoo-pg dropdb -U odoo "$db" >/dev/null 2>&1 || true
}
trap cleanup EXIT

set +e
docker run --rm --network aipbx-odoo -e HOST=aipbx-odoo-pg -e USER=odoo -e PASSWORD=odoo \
  -v "$addons:/mnt/extra-addons:ro" "odoo:$v" \
  odoo -d "$db" \
  -i aipbx_connector,aipbx_connector_crm $demo --test-enable --test-tags /aipbx_connector,/aipbx_connector_crm --stop-after-init --log-level=test 2>&1 \
  | grep -E " (WARNING|ERROR|CRITICAL) |Traceback|File \"|odoo.tests.result|tests.stats" | grep -v "Password reset" | tail -60
exit "${PIPESTATUS[0]}"
