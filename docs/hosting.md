# Host Applied Opt

This repository contains the web server, dashboard, Docker image, HTTPS proxy configuration, and a Render deployment Blueprint. A single origin serves the dashboard and the Python analysis API. Pasted source reaches the AST reviewer in this server; source is neither executed nor saved.

The hosted entry point is `applied-opt host` (also `python -m applied_opt.hosting`). It runs the WSGI application with Gunicorn. The existing `applied-opt serve` command remains available for local development. Core CLI tools support Python 3.11+; use Python 3.12+ on a Unix-like system for hosting, following Gunicorn's [installation guidance](https://gunicorn.org/install/). The supplied Docker image uses Python 3.12.

## 1. Run the complete web app locally

Install Docker with Compose v2 and Python 3, clone this repository, and run:

```bash
git clone https://github.com/thuygom/applied-opt.git
cd applied-opt
python3 deploy/bootstrap.py --up
```

The bootstrap creates `.env` with a random API token and starts `compose.yaml`. It creates the file with owner-only permissions and never replaces an existing file. The token is not printed. Running without `--up` prepares the configuration without starting containers.

Open **http://localhost:8787**, paste the `APPLIED_OPT_TOKEN` value from `.env` into the dashboard's token field, and run a review. Use that exact hostname: the configured public origin is `localhost`, while `127.0.0.1` is available for internal health checks. The local Compose file publishes only on loopback.

Useful commands:

```bash
docker compose ps
docker compose logs --tail=100 applied-opt
curl --fail http://localhost:8787/healthz
docker compose down
```

`/healthz` is public and identifies the service. The report and analysis APIs require the token. An unauthenticated `curl -i http://localhost:8787/api/report` should return `401`.

## 2. Host on a VPS with automatic HTTPS

Point a DNS hostname you control, such as `opt.example.com`, to your server and allow inbound TCP ports 80 and 443. UDP 443 is optional for HTTP/3. Install Docker with Compose v2 and Python 3, clone this repository on that server, then run:

```bash
python3 deploy/bootstrap.py --domain opt.example.com --up
```

This creates **`.env.production`** and selects the standalone **`compose.production.yaml`**. Existing configuration is preserved, and the helper refuses to silently switch an existing production file to a different domain. `deploy/Caddyfile` sends both dashboard and API requests to the same application container. Caddy manages TLS certificates and HTTP-to-HTTPS redirects; the `caddy_data` and `caddy_config` named volumes persist its state. DNS, externally reachable ports, and persistent writable certificate storage are prerequisites described in [Caddy's automatic HTTPS documentation](https://caddyserver.com/docs/automatic-https).

Open `https://opt.example.com` and enter the token stored in `.env.production`. The application has no published host port in this stack; only Caddy is exposed.

The exact equivalent start command is:

```bash
docker compose --env-file .env.production -f compose.production.yaml up --build -d
```

Use that same file selection for inspection and maintenance:

```bash
docker compose --env-file .env.production -f compose.production.yaml ps
docker compose --env-file .env.production -f compose.production.yaml logs --tail=100
curl --fail https://opt.example.com/healthz
docker compose --env-file .env.production -f compose.production.yaml down
```

After reviewing and pulling a new repository revision, rerun the start command to rebuild. Avoid `down --volumes` when you intend to retain certificate state. This production file is complete on its own; combining it with the local Compose file would also publish the local app port.

## 3. Deploy the repository on Render

[Deploy to Render](https://render.com/deploy?repo=https://github.com/thuygom/applied-opt)

The root `render.yaml` defines a Docker **web service**. Select the repository through that link, review the Blueprint in your own Render account, and create it. The Blueprint chooses the free plan for an initial deployment; review provider limits and your service requirements before using it for continuous reporting. The repository does not provision an account or purchase capacity.

Render builds this repository's Dockerfile. The Blueprint generates `APPLIED_OPT_TOKEN`, uses `/healthz`, and sets `PORT=10000`. The server resolves its public origin from Render's supplied `RENDER_EXTERNAL_URL`. After the service becomes healthy, open its `https://…onrender.com` URL, retrieve the generated token from the service's Environment settings, and enter it in the dashboard. These fields follow the official [Blueprint specification](https://render.com/docs/blueprint-spec) and [default environment variables](https://render.com/docs/environment-variables).

Automatic redeployment is disabled in the template so that an upstream commit does not automatically update everyone who deployed it. Use **Manual Deploy** for a reviewed revision, or enable automatic deployment for your own fork. This follows Render's [Deploy to Render button guidance](https://render.com/docs/deploy-to-render).

For a custom domain, configure it with Render and set `APPLIED_OPT_PUBLIC_URL=https://opt.example.com`. That configured origin becomes the one accepted by the application; use it consistently. This release supports one public hostname per deployment. Render's [HTTP health checks](https://render.com/docs/health-checks) select a verified custom domain when one exists, so avoid adding aliases that the application does not accept. Do not set `APPLIED_OPT_PUBLIC_URL` to an internal container address. Render terminates HTTPS, and the backend uses the declared public origin rather than trusting arbitrary forwarded headers.

## Configuration reference

| Variable | Hosted behavior |
| --- | --- |
| `APPLIED_OPT_TOKEN` | Required, at least 16 printable ASCII characters without spaces. Use a generated secret. A single shared bearer token protects reports and analysis endpoints. |
| `APPLIED_OPT_PUBLIC_URL` | Required public origin, such as `https://opt.example.com` or `http://localhost:8787`. No path prefix. Falls back to `RENDER_EXTERNAL_URL` when omitted. |
| `APPLIED_OPT_REPORT` | Optional path to one readable JSON report. The Docker image defaults to `/app/examples/demo-report.json`. |
| `HOST` | Bind interface; default `0.0.0.0` in hosting mode. |
| `PORT` | Internal listening port; default `8787`. Render Blueprint uses `10000`. |
| `WEB_CONCURRENCY` | Gunicorn worker processes; default `2`, explicitly `1` in the free Render template. |
| `APPLIED_OPT_THREADS` | Threads per worker; default `4`. |
| `DOMAIN` | DNS hostname used only by the VPS Compose/Caddy configuration. |

The server fails startup on missing/invalid hosted configuration. The declared public origin controls allowed Host and browser Origin headers, independently of the internal listening port. Forwarding a request from another site does not establish trust. If adding an existing reverse proxy, preserve the original Host header, terminate HTTPS there, and keep the backend port private. Gunicorn's [deployment guidance](https://gunicorn.org/deploy/) describes its proxy deployment model.

## Use your report

The image includes a synthetic demonstration report so the first deployment is usable. In either Compose file, replace the read-only bind mount's left side with a report path on your server, keeping `/data/report.json` as the container path:

```yaml
volumes:
  - "./artifacts/my-report.json:/data/report.json:ro"
```

Create the report file before starting the container and allow the image's non-root UID `10001` to read it. Keep private reports outside a public Git repository. On Render, you can import a report through the dashboard for the current browser session, or supply a report through a provider-managed file/disk and set `APPLIED_OPT_REPORT` to its path. Browser imports are not persisted on the server. The server supports one configured report file, not a shared report database or upload store.

## Verify a deployment

1. Open the configured URL and check that `/healthz` returns `200` with `status: "ok"`.
2. Check that `/api/report` rejects a request without a bearer token (`401`).
3. Enter the token in the dashboard and load the bundled or configured report.
4. Paste Python with two sequential `await` calls, run the review, and confirm line-level findings appear. Use **Python 소스에서 흐름 만들기** (create flow from Python source) in the flow view to compile a diagram through the same server.
5. Restart the application, revisit the same origin, and repeat the authenticated review. Token entry is kept only in the current page session and may need to be repeated.

Local automated checks cover the server and configuration contracts. A successful local test does not mean that a Render account, DNS record, or certificate has been provisioned. Check provider deployment logs and the steps above on your own target after provisioning.

## Data and access boundaries

The public HTML shell contains no API token. Hosted API requests use the token from the dashboard field; it is not saved to browser storage. Submitted source is parsed with bounded inputs, not imported or executed. Hosting does not add AI model calls, a repository runner, user accounts, tenant isolation, or benchmark infrastructure. This is a shared-token reporting/review service; use an organizational access proxy when distinct user access is required. See [SECURITY.md](../SECURITY.md).

The bundled measurements use synthetic delays and deterministic checks. Deploying the dashboard does not establish an LLM quality result, a production latency improvement, or a 15-second SLA.
