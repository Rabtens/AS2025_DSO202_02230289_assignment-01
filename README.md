# DSO202 - Assignment 1: Three-Tier Application Deployment on Kubernetes
**Namespace:** `dso202-assignment-01` · **Cluster:** local `kind` cluster (Practical 1) · **Scope:** Unit I

---

## 0. Prerequisite check

Confirm the `dso202` `kind` cluster from Practical 1 is up (recreate it from `cluster/kind-cluster.yaml` if it was torn down at the end of that practical).

```bash
kubectl cluster-info --context kind-dso202
kubectl get nodes -o wide
docker ps --filter "name=dso202-control-plane"
```
![alt text](<evidence/Screenshot from 2026-09-07 10-33-47.png>)

![alt text](<evidence/Screenshot from 2026-09-07 10-33-59.png>)

![alt text](<evidence/Screenshot from 2026-09-07 10-34-15.png>)

`frontend-svc` (Task 5) is published on nodePort `30080`. Whether that port is reachable from the host depends on whether this `kind` cluster was created with a matching `extraPortMappings` entry - check before relying on it:

```bash
docker ps --filter "name=dso202-control-plane" --format '{{.Ports}}'
```

On the cluster used for this submission the output is `127.0.0.1:44215->6443/tcp` only - the API server port and nothing else - so host port `30080` is **not** mapped and `http://localhost:30080` does not resolve. Every browser/`curl` step below therefore uses `kubectl port-forward`, which needs no cluster re-creation. If your `kind-cluster.yaml` does map `30080`, you can substitute `http://localhost:30080` for the forwarded port anywhere below.

---

## 1. Repository layout

```
assignment-1/
├── namespace.yaml
├── configmap.yaml
├── secret.yaml
├── quota.yaml
├── database/
│   ├── pvc.yaml
│   ├── deployment.yaml
│   └── service.yaml
├── backend/
│   ├── deployment.yaml
│   └── service.yaml
├── frontend/
│   ├── deployment.yaml
│   ├── nginx-configmap.yaml
│   └── service.yaml
├── bonus-rbac/
│   └── rbac.yaml          
└── README.md              
```

---

## 2. Task 1 - Namespace and Architecture Note

### 2.1 Architecture note 

**Control-plane components involved:** the **API server** receives every `kubectl apply`/`create` call in this assignment and persists the desired state to **etcd**. The **scheduler** watches for the three unscheduled Pods created by the Deployments and binds each to the single `kind` worker node (there is only one node in this cluster, so no real scheduling contention). The **controller manager** runs the Deployment and ReplicaSet controllers, which is what recreates the backend Pod during the Task 7c self-healing test.

**Node-level components involved:** on the one `kind` node, the **kubelet** pulls each of the three images and starts the containers via the container runtime, reports Pod status back to the API server, and executes `kubectl exec` (Task 7b) directly against the running container. **kube-proxy** programs the iptables/IPVS rules that make the `ClusterIP` and headless Services routable, and **CoreDNS** (running as its own Deployment in `kube-system`) is what resolves `db-svc` and `backend-svc` by name - this is exactly what Task 7b verifies.

**Object choice per tier, and why:**

| Tier | Object | Why |
|---|---|---|
| Database | `Deployment` (replicas: 1) + `PersistentVolumeClaim` + headless `Service` | A `Deployment` gives self-healing (Task 7c) even at one replica; a bare Pod would not. The PVC decouples the Postgres data directory from the Pod's lifecycle - required so data survives the Task 7c Pod deletion. The Service is headless (`clusterIP: None`) because the backend needs a single stable DNS name for the one database Pod, not a load-balanced virtual IP. |
| Backend | `Deployment` + `ClusterIP` `Service` | Stateless API tier - a normal `ClusterIP` Service load-balances across replicas (currently 1) and is only reachable inside the namespace, satisfying the "never external" constraint. |
| Frontend | `Deployment` + `NodePort` `Service` | Only tier that needs to leave the cluster boundary, so it is the only one given a `NodePort`. |
| Namespace-wide | `ConfigMap`, `Secret`, `ResourceQuota`, `LimitRange` | Separates non-sensitive config from credentials (Task 2), and bounds all three Deployments' resource consumption on a single shared node (Task 6). |

### 2.2 Commands

```bash
kubectl apply -f namespace.yaml
kubectl get namespace dso202-assignment-01 --show-labels
```
![alt text](<evidence/Screenshot from 2026-09-07 10-43-48.png>)

---

## 3. Task 2 - Configuration and Secrets

`configmap.yaml` holds every non-sensitive value (`DB_HOST`, `DB_PORT`, `DB_NAME`, `APP_PORT`, `CORS_ORIGIN`, `POSTGRES_DB`, `BACKEND_URL`). `secret.yaml` holds every credential (`DB_USER`, `DB_PASSWORD`, `POSTGRES_USER`, `POSTGRES_PASSWORD`), base64-encoded rather than typed as literal plaintext strings in the committed manifest.

```bash
kubectl apply -f configmap.yaml
kubectl apply -f secret.yaml
kubectl get configmap task-tracker-config -n dso202-assignment-01 -o yaml
kubectl get secret task-tracker-secret -n dso202-assignment-01 -o yaml
```

![alt text](<evidence/Screenshot from 2026-09-07 10-46-03.png>)

![alt text](<evidence/Screenshot from 2026-09-07 10-46-25.png>)

![alt text](<evidence/Screenshot from 2026-09-07 10-46-36.png>)

**Required documentation note (Secret encoding caveat):** the output of the second `get secret ... -o yaml` shows the credential values as base64 strings, e.g.:

```bash
echo -n 'TaskP@ss2026' | base64      # -> VGFza1BAc3MyMDI2
echo 'VGFza1BAc3MyMDI2' | base64 -d  # -> TaskP@ss2026
```
![alt text](<evidence/Screenshot from 2026-09-07 10-49-06.png>)

Base64 is an **encoding**, not encryption - anyone with `kubectl get secret -o yaml` access, or direct etcd access, can reverse it in one command as shown above. This `kind` cluster does not have encryption-at-rest enabled for etcd, so the Secret is not actually protected beyond RBAC controlling who can read it. This is documented rather than "fixed" because enabling an encryption provider for etcd is out of the Unit I scope of this assignment.

---

## 4. Task 3 - Database Tier

```bash
kubectl apply -f database/pvc.yaml
kubectl get pvc db-pvc -n dso202-assignment-01
kubectl get storageclass
```
![alt text](<evidence/Screenshot from 2026-09-07 10-51-17.png>)

```bash
kubectl apply -f database/deployment.yaml
kubectl apply -f database/service.yaml
kubectl get pods -n dso202-assignment-01 -l tier=database
kubectl get svc db-svc -n dso202-assignment-01
kubectl describe svc db-svc -n dso202-assignment-01
```
![alt text](<evidence/Screenshot from 2026-09-07 10-52-50.png>)

![alt text](<evidence/Screenshot from 2026-09-07 10-53-00.png>)

Confirm `db-svc` shows `Type: ClusterIP` and `Cluster-IP: None` - this is what makes it headless, and `describe` should list the database Pod's IP directly under `Endpoints`.

```bash
kubectl logs -n dso202-assignment-01 deployment/db-deployment
```
![alt text](<evidence/Screenshot from 2026-09-07 11-21-03.png>)

Check that the log shows Postgres finishing initialisation and running the seed script from `01-init.sql` (three sample rows in the `tasks` table) without a `POSTGRES_PASSWORD ... not set` error - that error would mean the Secret keys weren't wired correctly.

---

## 5. Task 4 - Backend Tier

```bash
kubectl apply -f backend/deployment.yaml
kubectl apply -f backend/service.yaml
kubectl get pods -n dso202-assignment-01 -l tier=backend
kubectl get svc backend-svc -n dso202-assignment-01
```
![alt text](<evidence/Screenshot from 2026-09-07 11-23-54.png>)

```bash
kubectl logs -n dso202-assignment-01 deployment/backend-deployment
```
![alt text](<evidence/Screenshot from 2026-09-07 11-24-16.png>)

Look for `[db] connected` followed by `[server] listening on :8080` in the log. If the log instead loops on `[db] not reachable yet`, re-check that `DB_HOST` in the ConfigMap matches the database Service's name (`db-svc`) exactly - this is the failure mode called out in the brief's troubleshooting table.

```bash
kubectl describe svc backend-svc -n dso202-assignment-01
```
![alt text](<evidence/Screenshot from 2026-09-07 11-26-08.png>)

Confirm `Type: ClusterIP` - the backend must never show `NodePort` or `LoadBalancer` here.

---

## 6. Task 5 - Frontend Tier

```bash
kubectl apply -f frontend/nginx-configmap.yaml
kubectl apply -f frontend/deployment.yaml
kubectl apply -f frontend/service.yaml
kubectl get pods -n dso202-assignment-01 -l tier=frontend
kubectl get svc frontend-svc -n dso202-assignment-01
```
![alt text](<evidence/Screenshot from 2026-09-07 11-28-12.png>)

Port-forward it for the demo (see Task 0 - host port 30080 is not mapped on this cluster):

```bash
kubectl port-forward -n dso202-assignment-01 svc/frontend-svc 8081:8080
# then browse to http://localhost:8081
```
![alt text](<evidence/Screenshot from 2026-09-07 11-51-54.png>)

![alt text](<evidence/Screenshot from 2026-09-07 11-46-41.png>)

![alt text](<evidence/Screenshot from 2026-09-07 11-46-59.png>)

### 6.1 Required fix - the browser cannot resolve `backend-svc`

On the first deployment the page rendered but every request failed: the status pill read **`backend unreachable`** and the ledger showed `could not load the ledger (NetworkError when attempting to fetch resource.)`.

This was **not** a backend or database fault. Both tiers were healthy the whole time, which `kubectl exec` from inside the frontend Pod confirms:

```bash
POD=$(kubectl get pod -n dso202-assignment-01 -l tier=frontend -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n dso202-assignment-01 $POD -- wget -qO- http://backend-svc:8080/api/status
# {"status":"ok","db":"connected"}
```
![alt text](<evidence/Screenshot from 2026-09-07 11-54-45.png>)

**Root cause.** The frontend image renders `BACKEND_URL` into `/usr/share/nginx/html/config.js` at container start (`docker-entrypoint.sh` runs `envsubst` over `config.js.template`), and `app.js` then builds every request as `` `${BACKEND_URL}/api/tasks` ``. That JavaScript executes **in the browser on the host machine**, not in the Pod. The original value was:

```js
window.__CONFIG__ = { BACKEND_URL: "http://backend-svc:8080" };
```

`backend-svc` is a cluster-internal DNS name served by CoreDNS. The browser sits outside the cluster and never talks to CoreDNS, so the name does not resolve and `fetch()` fails before a request is ever sent - which is exactly the `NetworkError` the UI reported. Pointing the browser straight at the backend is also not an option here, because the brief forbids exposing `backend-svc` beyond `ClusterIP`.

**Fix - proxy the API through the frontend's own origin.** `frontend/nginx-configmap.yaml` supplies a replacement `/etc/nginx/nginx.conf` that adds one location block:

```nginx
location /backend/ {
  proxy_pass http://backend-svc:8080/;
  ...
}
```

and `BACKEND_URL` in `configmap.yaml` becomes the same-origin path `/backend` instead of a URL. `frontend/deployment.yaml` mounts that ConfigMap over `nginx.conf` with `subPath`, so only that single file is replaced rather than masking all of `/etc/nginx`.

The request path is now: browser -> `http://localhost:8081/backend/api/tasks` (the same origin the page was loaded from) -> nginx **inside the cluster**, which *can* resolve CoreDNS names -> `http://backend-svc:8080/api/tasks`. The trailing slash on `proxy_pass` strips the `/backend` prefix, so the backend sees its own unchanged paths.

Why this fix rather than the alternatives:

- `backend-svc` stays `ClusterIP`-only - the non-negotiable constraint in the brief is preserved, because the browser still only ever talks to the frontend.
- No CORS is involved: the API is now same-origin, so `CORS_ORIGIN` never has to be relied on.
- No hostname or port is hardcoded anywhere, so the identical manifests work through `kubectl port-forward` on any port *and* through nodePort 30080 on a cluster that maps it.
- The alternative - a second `port-forward` onto `svc/backend-svc` with `BACKEND_URL: "http://localhost:8080"` - would make the deployed app depend on a developer's laptop keeping a tunnel open, which is not a property of the deployment itself.

### 6.2 Verification

```bash
# the value now shipped to the browser
curl -s http://localhost:8081/config.js
```
![alt text](<evidence/Screenshot from 2026-09-07 19-38-03.png>)

```js
window.__CONFIG__ = {
  BACKEND_URL: "/backend"
};
```

```bash
# the same-origin API calls the browser makes, proxied through to backend-svc
curl -s http://localhost:8081/backend/api/status
# {"status":"ok","db":"connected"}

curl -s http://localhost:8081/backend/api/tasks
# [{"id":1,...},{"id":2,...},{"id":3,...}]
```
![alt text](<evidence/Screenshot from 2026-09-07 19-39-55.png>)

Reloading `http://localhost:8081` now shows the status pill as **`backend + db online`** and the three seeded tasks in the ledger.

![alt text](<evidence/Screenshot from 2026-09-07 19-41-41.png>)

> Note: `nginx.conf` is mounted with `subPath`, so edits to `frontend/nginx-configmap.yaml` are **not** hot-reloaded into the running Pod. Re-apply and restart:
> ```bash
> kubectl apply -f frontend/nginx-configmap.yaml
> kubectl rollout restart deployment/frontend-deployment -n dso202-assignment-01
> ```

![alt text](<evidence/Screenshot from 2026-09-07 19-42-19.png>)

---

## 7. Task 6 - Resource Governance

```bash
kubectl apply -f quota.yaml
kubectl describe resourcequota dso202-a1-quota -n dso202-assignment-01
kubectl describe limitrange dso202-a1-limits -n dso202-assignment-01
```

![alt text](<evidence/Screenshot from 2026-09-07 19-44-17.png>)

![alt text](<evidence/Screenshot from 2026-09-07 19-44-28.png>)

### Justification 

- **`pods: "6"`** - the three Deployments run 3 Pods at steady state (1 replica each). The quota allows double that so a rolling update (old Pod + new Pod briefly coexisting) is never blocked by the quota itself, while still capping runaway replica counts on a single-node cluster.
- **CPU/memory totals are derived from the `LimitRange` defaults, not guessed separately.** `LimitRange` gives every container a default limit of `500m` CPU / `512Mi` memory. At up to 6 Pods, that's `6 × 500m = 3` CPU and `6 × 512Mi = 3072Mi`, which is exactly `limits.cpu`/`limits.memory` in the quota. The same arithmetic at the default **request** (`250m`/`256Mi`) gives `1500m`/`1536Mi`, matching `requests.cpu`/`requests.memory`. The two objects agree with each other by construction rather than being two independent guesses.
- **`persistentvolumeclaims: "1"`** - only the database tier needs storage in this assignment; capping at 1 stops an accidental second PVC from silently consuming disk on the node.
- **`LimitRange` min/max (`50m`/`64Mi` to `1`/`1Gi`)** - wide enough that nginx, the small Node.js API, and Postgres all fit comfortably under their own default, but tight enough that no single misconfigured container can claim the whole node's CPU/memory on a laptop-hosted `kind` cluster.

---

## 8. Task 7 - Verification and Interactivity

### 7a. Full CRUD cycle

```bash
kubectl port-forward -n dso202-assignment-01 svc/backend-svc 8082:8080
```
![alt text](<evidence/Screenshot from 2026-09-07 20-01-52.png>)

In a second terminal:

```bash
# CREATE
curl -s -X POST http://localhost:8082/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"title":"Submit Assignment 1","description":"DSO202 Unit I","status":"pending"}'

# READ (list)
curl -s http://localhost:8082/api/tasks

# READ (single) -- replace 4 with the id returned by the CREATE call above
curl -s http://localhost:8082/api/tasks/4

# UPDATE
curl -s -X PUT http://localhost:8082/api/tasks/4 \
  -H "Content-Type: application/json" \
  -d '{"status":"in_progress"}'

# DELETE
curl -s -X DELETE http://localhost:8082/api/tasks/4
```
![alt text](<evidence/Screenshot from 2026-09-07 19-56-31.png>)

![alt text](<evidence/Screenshot from 2026-09-07 20-02-03.png>)

![alt text](<evidence/Screenshot from 2026-09-07 20-02-37.png>)

![alt text](<evidence/Screenshot from 2026-09-07 20-08-41.png>)

![alt text](<evidence/Screenshot from 2026-09-07 20-09-02.png>)

### 7b. Service DNS resolution from inside a Pod

```bash
kubectl get pods -n dso202-assignment-01 -l tier=frontend
# copy the Pod name from the output above into the command below
kubectl exec -n dso202-assignment-01 <frontend-pod-name> -- curl -s http://backend-svc:8080/api/status
```
![alt text](<evidence/Screenshot from 2026-09-07 20-14-21.png>)

Expect `{"status":"ok","db":"connected"}`. This proves CoreDNS resolved the bare Service name `backend-svc` to a cluster IP from inside the frontend container, with no hardcoded IP anywhere.



### 7c. Self-healing and data persistence

```bash
# create a task we'll check for after the deletion
curl -s -X POST http://localhost:8082/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"title":"Survive the Pod deletion","status":"pending"}'
```
![alt text](<evidence/Screenshot from 2026-09-07 20-16-24.png>)

Note the `id` returned, then in one terminal:

```bash
kubectl get pods -n dso202-assignment-01 -l tier=backend --watch
```

![alt text](<evidence/Screenshot from 2026-09-07 20-17-42.png>)

In a second terminal:

```bash
kubectl delete pod -n dso202-assignment-01 -l tier=backend
```
![alt text](<evidence/Screenshot from 2026-09-07 20-17-48.png>)

Once the new Pod is `Running`, re-forward if the port-forward dropped, then re-fetch the task by its `id`:

```bash
ID=$(curl -s -X POST http://localhost:8082/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"title":"Rollout check","description":"DSO202 Unit I","status":"pending"}' \
  | grep -o '"id":[0-9]*' | cut -d: -f2)
curl -s http://localhost:8082/api/tasks/$ID

```
![alt text](<evidence/Screenshot from 2026-09-07 20-25-30.png>)


### 7d. Declarative vs. imperative comparison

Using a throwaway ConfigMap (not the real `task-tracker-config`, so the comparison doesn't disturb the running app):

```bash
# Declarative
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: demo-cm
  namespace: dso202-assignment-01
data:
  greeting: "hello-declarative"
EOF
kubectl get configmap demo-cm -n dso202-assignment-01 -o yaml
kubectl delete configmap demo-cm -n dso202-assignment-01

# Imperative equivalent
kubectl create configmap demo-cm \
  --from-literal=greeting=hello-imperative \
  -n dso202-assignment-01
kubectl get configmap demo-cm -n dso202-assignment-01 -o yaml
kubectl delete configmap demo-cm -n dso202-assignment-01
```
![alt text](<evidence/Screenshot from 2026-09-07 20-26-58.png>)

![alt text](<evidence/Screenshot from 2026-09-07 20-28-20.png>)

**Comparison:** the declarative form (`apply -f`) describes the desired end state in a YAML file that is version-controlled, diffable, and re-runnable - applying it twice is a no-op, and it is what the whole rest of this assignment is built on. The imperative form (`create configmap --from-literal=...`) is faster to type for a one-off, throwaway object, but the command itself isn't saved anywhere by default, so there is no record of *why* the object looks the way it does, and re-running the exact same command a second time fails with `AlreadyExists` instead of converging. For anything that needs to be reproducible, reviewed, or rolled back - i.e. everything else in this assignment - declarative is the correct default; imperative is reserved for quick, disposable, ad-hoc checks like this one.

---

## 9. Task 8 - Namespace RBAC 

```bash
kubectl apply -f bonus-rbac/rbac.yaml
kubectl auth can-i list pods \
  --as=system:serviceaccount:dso202-assignment-01:dso202-a1-viewer \
  -n dso202-assignment-01
kubectl auth can-i delete pods \
  --as=system:serviceaccount:dso202-assignment-01:dso202-a1-viewer \
  -n dso202-assignment-01
```
![alt text](<evidence/Screenshot from 2026-09-07 20-31-31.png>)

Expect `yes` for the first command and `no` for the second — the ServiceAccount can read namespace objects but not modify or delete them.

---

## 10. Full apply order (for a clean re-run)

```bash
kubectl apply -f namespace.yaml
kubectl apply -f configmap.yaml
kubectl apply -f secret.yaml
kubectl apply -f quota.yaml
kubectl apply -f database/pvc.yaml
kubectl apply -f database/deployment.yaml
kubectl apply -f database/service.yaml
kubectl apply -f backend/deployment.yaml
kubectl apply -f backend/service.yaml
kubectl apply -f frontend/nginx-configmap.yaml
kubectl apply -f frontend/deployment.yaml
kubectl apply -f frontend/service.yaml
kubectl apply -f bonus-rbac/rbac.yaml   # optional
kubectl get all -n dso202-assignment-01
```

![alt text](<evidence/Screenshot from 2026-09-07 20-33-54.png>)

---

## 11. Submission checklist

- [x] Namespace created and all resources scoped to it
- [x] ConfigMap and Secret created with matching key values across both naming conventions (`DB_*` / `POSTGRES_*`)
- [x] Database Deployment mounts the PVC and uses a headless Service
- [x] Backend Deployment uses a ClusterIP Service and the correct environment variables
- [x] Frontend Deployment uses a NodePort Service and the correct `BACKEND_URL`
- [x] ResourceQuota and LimitRange applied and justified 
- [x] Full CRUD cycle demonstrated and evidenced 
- [x] Service DNS resolution demonstrated from inside a Pod 
- [x] Pod self-healing and data persistence demonstrated 
- [x] One resource created both declaratively and imperatively, with written comparison
- [x] README complete with all required notes
- [x] All manifests committed to the designated version-control repository# AS2025_DSO202_02230289_assignment-01
