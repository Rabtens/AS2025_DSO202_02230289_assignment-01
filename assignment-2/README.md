# DSO202 - Assignment 2: Unit II Concepts Applied to the Assignment 1 Task Tracker
**Namespace:** `dso202-assignment-02` · **Cluster:** local `kind` cluster `dso202` (Kubernetes v1.36.1) · **Scope:** Unit II (§2.1–§2.5)

Assignment 1 deployed the three-tier Task Tracker (Postgres → Node.js API → nginx UI) using Unit I objects only. This assignment re-deploys the **same three images** into a new namespace and rebuilds the platform around them with every Unit II topic:

| Unit II topic | What changed in the Task Tracker | Files |
|---|---|---|
| **2.1 StatefulSets** | Database moves from `Deployment` + hand-made PVC to a `StatefulSet` with a headless Service, `volumeClaimTemplates`, an explicit PVC retention policy and a partitioned (canary) rolling update | `database/` |
| **2.2 Ingress** | The frontend `NodePort` and the in-Pod nginx proxy from A1 are replaced by one NGINX Ingress entry point with TLS termination, host-based routing and a path rewrite for `/backend` | `ingress/`, `frontend/`, `backend/` |
| **2.3 RBAC** | Per-workload ServiceAccounts with no API token; a developer Role (no Secrets), an auditor bound to the built-in `view` ClusterRole, a CI deployer pinned with `resourceNames`; all proven with `kubectl auth can-i` | `rbac/` |
| **2.4 Operators** | A `TaskBackup` CRD and a custom controller (list/watch → workqueue → reconcile) that turns each `TaskBackup` into a `pg_dump` CronJob + backup PVC, with status and ownerReference GC | `operator/` |
| **2.5 Istio** | Sidecars in every tier, STRICT mTLS, default-deny `AuthorizationPolicy` by ServiceAccount identity, retries/timeouts/circuit breaking, and header-scoped fault injection | `istio/` |

Assignment 1 (`dso202-assignment-01`) is left running untouched.

---

## 0. Prerequisites and environment notes

```bash
kubectl cluster-info --context kind-dso202
kubectl get nodes -o wide
docker info --format '{{.MemTotal}}'
```

**Resource constraint.** This cluster is a single `kind` node inside Docker Desktop with **1.7 GiB** of memory. Before starting, the node was already under enough memory pressure that `kube-scheduler` and `kube-controller-manager` were repeatedly losing their leader-election lease (`"Failed to renew lease" ... context deadline exceeded`) and restarting. A second, unrelated kind cluster (`kind-control-plane`, ~518 MiB) was stopped (`docker stop kind-control-plane`, reversible with `docker start`) to make room for Istio. Every resource figure in this assignment (istiod, sidecars, quota) is sized for this constraint and the reasoning is in the files.

**Tool versions pinned:** Istio **1.30.5** (Helm charts `istio/base`, `istio/istiod`), ingress-nginx controller **v1.15.1** (kind provider manifest).

---

## 1. Repository layout

```
assignment-2/
├── namespace.yaml              # istio-injection=enabled
├── configmap.yaml              # DB_HOST now db-0.db-headless (StatefulSet identity)
├── secret.yaml
├── quota.yaml                  # ResourceQuota + LimitRange, recalculated incl. sidecars
├── database/
│   ├── headless-service.yaml   # §2.1 clusterIP: None
│   └── statefulset.yaml        # §2.1 volumeClaimTemplates, partition, retention policy
├── backend/
│   ├── deployment.yaml         # 2 replicas, startup/liveness/readiness probes
│   └── service.yaml
├── frontend/
│   ├── deployment.yaml         # 2 replicas, stock nginx.conf (proxy moved to Ingress)
│   └── service.yaml            # NodePort -> ClusterIP
├── ingress/
│   ├── ingress.yaml            # §2.2 TLS, host rule, /backend rewrite
│   ├── generate-tls.sh         # self-signed cert -> Secret tasktracker-tls
│   └── controller-mesh-patch.yaml  # puts the NGINX controller in the mesh
├── rbac/
│   ├── serviceaccounts.yaml    # §2.3 one SA per tier, no token mounted
│   ├── developer-role.yaml     # Role + RoleBinding to a Group
│   ├── auditor-binding.yaml    # ClusterRole "view" via RoleBinding
│   ├── ci-deployer.yaml        # SA + Role pinned with resourceNames
│   └── verify-rbac.sh          # kubectl auth can-i test matrix
├── operator/
│   ├── crd.yaml                # §2.4 TaskBackup CRD (schema, defaults, CEL, status)
│   ├── controller.py           # the operator (Python stdlib only)
│   ├── rbac.yaml               # operator + backup-runner identities
│   ├── deployment.yaml
│   └── taskbackup.yaml         # a Custom Resource
├── istio/
│   ├── istiod-values.yaml      # §2.5 control plane sizing, native sidecars
│   ├── security.yaml           # STRICT mTLS + AuthorizationPolicies
│   ├── traffic.yaml            # DestinationRules + VirtualServices
│   └── fault-injection-demo.yaml
└── evidence/                   # captured command output for every step
```

---

<!-- SECTIONS BELOW ARE FILLED IN FROM THE LIVE RUN -->
