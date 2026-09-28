#!/usr/bin/env bash
# Checks every RBAC boundary in this folder with `kubectl auth can-i`
# impersonation (--as / --as-group). Prints PASS/FAIL per expectation and
# exits non-zero if any boundary is wrong.
set -uo pipefail
NS=dso202-assignment-02
fail=0

check() {  # check <expected yes|no> <description> <can-i args...>
  local want=$1 desc=$2; shift 2
  local got
  got=$(kubectl auth can-i "$@" 2>/dev/null)
  if [[ "$got" == "$want" ]]; then r=PASS; else r=FAIL; fail=1; fi
  printf '%-4s  want=%-3s got=%-3s  %s\n' "$r" "$want" "$got" "$desc"
}

DEV=(--as=dev-kuenzang --as-group=dso202-developers)
AUD=(--as=auditor@cst.edu.bt)
CI=(--as=system:serviceaccount:$NS:ci-deployer)
BE=(--as=system:serviceaccount:$NS:backend-sa)
OP=(--as=system:serviceaccount:$NS:taskbackup-operator)

echo "== developer (Group dso202-developers) =="
check yes "read pod logs"                    get pods/log               -n $NS "${DEV[@]}"
check yes "exec into pods"                   create pods/exec           -n $NS "${DEV[@]}"
check yes "rollout restart backend"          patch deployment/backend   -n $NS "${DEV[@]}"
check no  "read the DB Secret"               get secret/task-tracker-secret -n $NS "${DEV[@]}"
check no  "delete the ledger PVC"            delete pvc/data-db-0       -n $NS "${DEV[@]}"
check no  "edit RBAC"                        create rolebindings        -n $NS "${DEV[@]}"
check no  "read pods in assignment-01"       get pods -n dso202-assignment-01 "${DEV[@]}"

echo "== auditor (ClusterRole view via RoleBinding) =="
check yes "list pods"                        list pods                  -n $NS "${AUD[@]}"
check no  "read secrets"                     get secrets                -n $NS "${AUD[@]}"
check no  "patch a deployment"               patch deployment/backend   -n $NS "${AUD[@]}"
check no  "list pods in kube-system"         list pods -n kube-system "${AUD[@]}"

echo "== ci-deployer ServiceAccount =="
check yes "patch deployment/backend"         patch deployment/backend   -n $NS "${CI[@]}"
check yes "patch statefulset/db"             patch statefulset/db       -n $NS "${CI[@]}"
check no  "patch a deployment not in list"   patch deployment/other     -n $NS "${CI[@]}"
check no  "create deployments"               create deployments         -n $NS "${CI[@]}"
check no  "delete deployment/backend"        delete deployment/backend  -n $NS "${CI[@]}"
check no  "read secrets"                     get secrets                -n $NS "${CI[@]}"

echo "== backend-sa (application identity) =="
check no  "list pods"                        list pods                  -n $NS "${BE[@]}"
check no  "read secrets"                     get secrets                -n $NS "${BE[@]}"

echo "== taskbackup-operator ServiceAccount =="
check yes "watch taskbackups"                watch taskbackups.dso202.cst.edu.bt -n $NS "${OP[@]}"
check yes "patch taskbackups/status"         patch taskbackups.dso202.cst.edu.bt --subresource=status -n $NS "${OP[@]}"
check yes "create cronjobs"                  create cronjobs.batch      -n $NS "${OP[@]}"
check no  "read secrets"                     get secrets                -n $NS "${OP[@]}"
check no  "create cronjobs in kube-system"   create cronjobs.batch -n kube-system "${OP[@]}"

exit $fail
