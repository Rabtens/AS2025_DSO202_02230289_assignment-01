#!/usr/bin/env python3
"""TaskBackup operator (DSO202 Unit II, section 2.4).

A deliberately small controller with no third-party dependencies (Python
standard library only), so every moving part named in the lecture notes is
visible in one file:

  list + watch  -> the "informer": a full LIST, then a long-lived WATCH
                   from the returned resourceVersion (section 0).
  workqueue     -> watch events only ENQUEUE a key; a separate worker
                   thread dequeues and reconciles. Duplicate keys collapse.
  resync        -> every RESYNC_SECONDS every object is re-enqueued, which
                   is how status.lastBackupTime is refreshed from the
                   CronJob without watching CronJobs too.
  reconcile     -> level-based and idempotent: read desired state from the
                   TaskBackup spec, server-side-apply the PVC and CronJob
                   that realise it, then write status. Running it twice in a
                   row changes nothing the second time.

Deleting a TaskBackup needs no code: the CronJob carries an ownerReference
to it, so the garbage collector deletes the CronJob (and its Jobs/Pods).
The backup PVC intentionally has NO ownerReference - dumps outlive the
TaskBackup that produced them, the same Retain thinking as the database
StatefulSet's persistentVolumeClaimRetentionPolicy.
"""

import json
import os
import queue
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

GROUP, VERSION, PLURAL, KIND = "dso202.cst.edu.bt", "v1", "taskbackups", "TaskBackup"
FIELD_MANAGER = "taskbackup-operator"
RESYNC_SECONDS = int(os.environ.get("RESYNC_SECONDS", "60"))
BACKUP_IMAGE = os.environ.get("BACKUP_IMAGE", "sarojsanyasi/dso202-db:1.0")
RUNNER_SA = os.environ.get("RUNNER_SERVICE_ACCOUNT", "taskbackup-runner")

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://{}:{}".format(os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"])
NAMESPACE = os.environ.get("WATCH_NAMESPACE") or open(f"{SA_DIR}/namespace").read().strip()
SSL_CTX = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")

CR_PATH = f"/apis/{GROUP}/{VERSION}/namespaces/{NAMESPACE}/{PLURAL}"


def log(msg, **kv):
    extra = " ".join(f"{k}={v}" for k, v in kv.items())
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg} {extra}".rstrip(), flush=True)


class ApiError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}: {body[:300]}")
        self.code, self.body = code, body


def request(method, path, body=None, content_type="application/json", timeout=30):
    # The token is re-read on every call: projected ServiceAccount tokens are
    # rotated by the kubelet, so caching it at start-up would expire.
    token = open(f"{SA_DIR}/token").read().strip()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        return urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace")) from None


def call(method, path, body=None, content_type="application/json"):
    with request(method, path, body, content_type) as resp:
        return json.load(resp)


def apply(path, obj):
    """Server-side apply: declare the whole desired object, let the API
    server compute the diff. Idempotent by construction."""
    return call("PATCH", f"{path}?fieldManager={FIELD_MANAGER}&force=true", obj,
                "application/apply-patch+yaml")  # JSON is valid YAML


# ---------------------------------------------------------------- workqueue
class WorkQueue:
    """Keys waiting to be reconciled. A key already queued is not queued
    twice, so a burst of events for one object costs one reconcile."""

    def __init__(self):
        self._q, self._pending, self._lock = queue.Queue(), set(), threading.Lock()

    def add(self, key):
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
        self._q.put(key)

    def get(self):
        key = self._q.get()
        with self._lock:
            self._pending.discard(key)
        return key


# ---------------------------------------------------------------- informer
def list_names():
    data = call("GET", CR_PATH)
    return [i["metadata"]["name"] for i in data.get("items", [])], data["metadata"]["resourceVersion"]


def informer(wq):
    """LIST, then WATCH from that resourceVersion; on expiry, start over."""
    while True:
        try:
            names, rv = list_names()
            for n in names:
                wq.add(n)
            log("informer: listed", count=len(names), resourceVersion=rv)
            path = f"{CR_PATH}?watch=1&allowWatchBookmarks=true&resourceVersion={rv}&timeoutSeconds=300"
            with request("GET", path, timeout=330) as stream:
                for line in stream:  # one JSON event per line, streamed
                    event = json.loads(line)
                    etype, obj = event["type"], event["object"]
                    if etype == "ERROR":  # usually 410 Gone: rv too old -> relist
                        log("informer: watch error, relisting", reason=obj.get("reason"))
                        break
                    if etype == "BOOKMARK":
                        continue
                    name = obj["metadata"]["name"]
                    log("informer: event", type=etype, name=name)
                    if etype != "DELETED":  # deletion is handled by ownerReference GC
                        wq.add(name)
        except Exception as e:  # network blip, API server restart, ...
            log("informer: error, retrying in 5s", error=repr(e))
            time.sleep(5)


def resync(wq):
    while True:
        time.sleep(RESYNC_SECONDS)
        try:
            for n in list_names()[0]:
                wq.add(n)
        except Exception as e:
            log("resync: error", error=repr(e))


# ---------------------------------------------------------------- reconcile
def desired_pvc(tb):
    name = tb["metadata"]["name"]
    return {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": f"{name}-backups", "namespace": NAMESPACE,
                     "labels": {"app": "task-tracker", "tier": "backup",
                                "dso202.cst.edu.bt/taskbackup": name}},
        "spec": {"accessModes": ["ReadWriteOnce"],
                 "resources": {"requests": {"storage": tb["spec"].get("storageSize", "512Mi")}}},
    }


BACKUP_SCRIPT = r"""
set -eu -o pipefail
ts=$(date -u +%Y%m%dT%H%M%SZ)
out=/backups/${PGDATABASE}-${ts}.sql.gz
echo "dumping ${PGDATABASE}@${PGHOST} -> ${out}"
pg_dump --no-owner --clean --if-exists | gzip > "${out}.partial"
mv "${out}.partial" "${out}"
# retention: keep the newest $RETAIN dumps, delete the rest
ls -1t /backups/*.sql.gz | tail -n +$((RETAIN + 1)) | xargs -r rm -f --
echo "kept:"; ls -l /backups
"""


def desired_cronjob(tb):
    meta, spec = tb["metadata"], tb["spec"]
    name, target = meta["name"], spec.get("target", {})
    labels = {"app": "task-tracker", "tier": "backup", "dso202.cst.edu.bt/taskbackup": name}
    secret = target.get("credentialsSecret", "task-tracker-secret")
    return {
        "apiVersion": "batch/v1", "kind": "CronJob",
        "metadata": {
            "name": f"{name}-backup", "namespace": NAMESPACE, "labels": labels,
            # The link that makes `kubectl delete taskbackup X` clean up.
            "ownerReferences": [{"apiVersion": f"{GROUP}/{VERSION}", "kind": KIND,
                                 "name": name, "uid": meta["uid"],
                                 "controller": True, "blockOwnerDeletion": True}],
        },
        "spec": {
            "schedule": spec["schedule"],
            "suspend": bool(spec.get("suspend", False)),
            "concurrencyPolicy": "Forbid",  # never two pg_dumps writing at once
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 1,
            "jobTemplate": {"spec": {
                "backoffLimit": 2,
                "activeDeadlineSeconds": 300,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "serviceAccountName": RUNNER_SA,
                        "restartPolicy": "Never",
                        "securityContext": {"runAsUser": 70, "runAsGroup": 70, "fsGroup": 70,
                                            "runAsNonRoot": True},
                        "containers": [{
                            "name": "pg-dump",
                            "image": BACKUP_IMAGE,
                            "command": ["/bin/sh", "-c", BACKUP_SCRIPT],
                            "env": [
                                {"name": "PGHOST", "value": target.get("host", "db-0.db-headless")},
                                {"name": "PGDATABASE", "value": target.get("database", "taskdb")},
                                {"name": "RETAIN", "value": str(spec.get("retain", 7))},
                                {"name": "PGUSER", "valueFrom": {"secretKeyRef": {"name": secret, "key": "DB_USER"}}},
                                {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": secret, "key": "DB_PASSWORD"}}},
                            ],
                            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                          "limits": {"cpu": "300m", "memory": "256Mi"}},
                            "volumeMounts": [{"name": "backups", "mountPath": "/backups"}],
                        }],
                        "volumes": [{"name": "backups",
                                     "persistentVolumeClaim": {"claimName": f"{name}-backups"}}],
                    },
                },
            }},
        },
    }


def write_status(tb, status):
    status["observedGeneration"] = tb["metadata"]["generation"]
    current = tb.get("status") or {}
    # Only write when something changed. Status writes emit MODIFIED watch
    # events; writing unconditionally would make the operator wake itself up
    # forever.
    if all(current.get(k) == v for k, v in status.items()):
        return
    call("PATCH", f"{CR_PATH}/{tb['metadata']['name']}/status", {"status": status},
         "application/merge-patch+json")
    log("status updated", name=tb["metadata"]["name"], phase=status.get("phase"))


def reconcile(name):
    try:
        tb = call("GET", f"{CR_PATH}/{name}")
    except ApiError as e:
        if e.code == 404:
            return  # deleted between enqueue and now; GC handles the rest
        raise
    if tb["metadata"].get("deletionTimestamp"):
        return

    pvc = desired_pvc(tb)
    cj = desired_cronjob(tb)
    try:
        apply(f"/api/v1/namespaces/{NAMESPACE}/persistentvolumeclaims/{pvc['metadata']['name']}", pvc)
        live = apply(f"/apis/batch/v1/namespaces/{NAMESPACE}/cronjobs/{cj['metadata']['name']}", cj)
    except ApiError as e:
        if e.code in (400, 403, 422):  # invalid spec or denied: report, don't retry-spin
            msg = json.loads(e.body).get("message", e.body) if e.body.startswith("{") else e.body
            write_status(tb, {"phase": "Error", "message": msg[:400]})
            log("reconcile: rejected", name=name, code=e.code)
            return
        raise

    last = (live.get("status") or {}).get("lastSuccessfulTime")
    status = {
        "phase": "Suspended" if tb["spec"].get("suspend") else "Ready",
        "message": f"CronJob {cj['metadata']['name']} scheduled '{tb['spec']['schedule']}', "
                   f"keeping {tb['spec'].get('retain', 7)} dumps on PVC {pvc['metadata']['name']}",
        "cronJobName": cj["metadata"]["name"],
        "pvcName": pvc["metadata"]["name"],
    }
    if last:
        status["lastBackupTime"] = last
    write_status(tb, status)


def worker(wq):
    while True:
        name = wq.get()
        try:
            reconcile(name)
        except Exception as e:
            # Transient failure (API unavailable, conflict): retry later.
            log("reconcile: failed, requeue in 10s", name=name, error=repr(e))
            threading.Timer(10, wq.add, args=(name,)).start()


def main():
    log("taskbackup-operator starting", namespace=NAMESPACE, resync=RESYNC_SECONDS)
    wq = WorkQueue()
    threading.Thread(target=informer, args=(wq,), daemon=True, name="informer").start()
    threading.Thread(target=resync, args=(wq,), daemon=True, name="resync").start()
    worker(wq)


if __name__ == "__main__":
    sys.exit(main())
