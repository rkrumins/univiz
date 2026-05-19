# Deploying dataviz to Kubernetes (GKE + Nginx ingress)

Two equivalent paths: raw manifests (`deploy/k8s`, Kustomize) or the Helm
chart (`deploy/helm/dataviz`). Both deploy the 6 app workloads
(`viz-service`, `graph-service`, `aggregation-controlplane`,
`aggregation-worker`, `stats-service`, `frontend`).

## 1. Prerequisites

- A GKE cluster + `kubectl` context pointing at it; `helm` v3 (for the
  chart path).
- Docker Hub account (or any registry) and `docker` for building images.
- Postgres, Redis, FalkorDB — either external/managed (Cloud SQL,
  Memorystore) **or** the in-cluster StatefulSets (`deploy/k8s/stores` /
  Helm `stores.*.enabled`).

## 2. Build & push images

No images are published for you. Build all 6 (+ optional `seed`) from the
repo root and push to your registry:

```sh
REGISTRY=docker.io ORG=<your-org> TAG=v1 sh deploy/build-images.sh
```

Or let CI do it: `.github/workflows/build-images.yml` builds & pushes on
push to the deploy branch / `v*` tags / manual dispatch. It needs repo
secrets **`DOCKERHUB_USERNAME`** and **`DOCKERHUB_TOKEN`**.

Point the manifests at your images: Helm `--set image.org=<your-org>
--set image.tag=v1` (registry/tag are values); raw manifests use
`docker.io/synodic/<name>:latest` — edit the `image:` lines or use a
Kustomize image override.

## 3. Install the ingress controller

GKE has no Nginx ingress by default:

```sh
helm upgrade --install ingress-nginx ingress-nginx \
  --repo https://kubernetes.github.io/ingress-nginx \
  --namespace ingress-nginx --create-namespace
```

## 4. Create the Secret

Never commit real secrets. Create it imperatively:

```sh
kubectl create namespace dataviz
kubectl -n dataviz create secret generic dataviz-secrets \
  --from-literal=MANAGEMENT_DB_URL='postgresql+asyncpg://synodic:PASS@PG_HOST:5432/synodic' \
  --from-literal=REDIS_URL='redis://REDIS_HOST:6379/0' \
  --from-literal=JWT_SECRET_KEY="$(openssl rand -hex 48)" \
  --from-literal=ADMIN_PASSWORD='change-me' \
  --from-literal=CREDENTIAL_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
```

Add extra keys depending on stores:
- **External Postgres**: add `DB_INIT_DSN` (admin libpq URL) so the
  `dataviz-db-init` Job can create the role + `aggregation` schema.
- **In-cluster Postgres** (`deploy/k8s/stores`): add `POSTGRES_PASSWORD`
  (matches the password in `MANAGEMENT_DB_URL`); Postgres self-bootstraps
  the schema, so skip the db-init Job (`dbInit.enabled=false`).

Keep `CREDENTIAL_ENCRYPTION_KEY` set in production — empty means stored
provider credentials are plaintext. Keep `CORS_ALLOWED_ORIGINS` (ConfigMap
/ `config.corsAllowedOrigins`) in sync with the ingress host.

## 5. Deploy

**Helm:**

```sh
helm install dataviz deploy/helm/dataviz -n dataviz \
  --set image.org=<your-org> --set image.tag=v1 \
  --set ingress.host=dataviz.example.com \
  --set config.corsAllowedOrigins=http://dataviz.example.com \
  --set secrets.create=false --set secrets.existingSecret=dataviz-secrets
# in-cluster stores instead of managed: add
#   --set stores.postgres.enabled=true --set stores.redis.enabled=true \
#   --set stores.falkordb.enabled=true --set dbInit.enabled=false
```

**Raw manifests:** edit `deploy/k8s/configmap.yaml` (FalkorDB host, CORS,
ingress host in `ingress.yaml`), then:

```sh
kubectl apply -k deploy/k8s
# optional in-cluster stores:
kubectl apply -k deploy/k8s/stores
```

The control plane is a singleton that runs Alembic and owns the
`aggregation` schema; `viz/worker/stats` have a `wait-for-controlplane`
initContainer so they don't start until it's healthy.

## 6. DNS & verification

```sh
kubectl -n ingress-nginx get svc ingress-nginx-controller   # external IP
# point dataviz.example.com at that IP, then:
curl http://dataviz.example.com/health        # {"status":"healthy","service":"frontend"}
curl http://dataviz.example.com/api/v1/health # 200 (proxied to viz-service)
kubectl -n dataviz get pods                    # all Ready
```

No DNS yet? `kubectl -n dataviz port-forward svc/frontend 8080:80` then
hit `http://localhost:8080/`.

## 7. Optional demo data

```sh
kubectl apply -f deploy/k8s/seed-job.yaml      # raw
# or Helm: --set seed.enabled=true
```

Idempotent — skips if the graph already has data.

## 8. Upgrade / rollback

- Helm: `helm upgrade dataviz deploy/helm/dataviz ...` /
  `helm rollback dataviz`.
- Raw: re-apply with a new `image.tag`; roll back with
  `kubectl -n dataviz rollout undo deployment/<name>`.
