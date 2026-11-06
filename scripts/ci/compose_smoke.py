"""Full Compose smoke on an isolated GitHub runner; no prune or volume deletion."""

import hashlib
import json
import os
import secrets
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

if os.getenv("GITHUB_ACTIONS") != "true":
    raise SystemExit(
        "Run only on an ephemeral GitHub Actions runner, never on production"
    )

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
WORK = ROOT / "work" / "compose-ci"
WORK.mkdir(parents=True, exist_ok=True)
ENV_FILE = WORK / "runtime.env"
ENV_FILE.write_text(
    "DEBUG=0\nSECRET_KEY=" + secrets.token_urlsafe(48) + "\n"
    "POSTGRES_DB=crm\nPOSTGRES_USER=crm\nPOSTGRES_PASSWORD="
    + secrets.token_hex(24)
    + "\n"
    "REDIS_URL=redis://redis:6379/0\n"
    "ALLOWED_HOSTS=localhost,127.0.0.1,testserver\n"
    "CSRF_TRUSTED_ORIGINS=http://127.0.0.1:18080,http://127.0.0.1:18081\n"
    "CRM_DOMAIN=:80\nPRIVATE_MEDIA_ROOT=/app/private_media\nCRM_COMPOSE_SMOKE=1\n"
)
ENV_FILE.chmod(0o600)
settings = dict(line.split("=", 1) for line in ENV_FILE.read_text().splitlines())
with ENV_FILE.open("a") as f:
    f.write(
        "DATABASE_URL=postgresql://crm:"
        + settings["POSTGRES_PASSWORD"]
        + "@db:5432/crm\n"
    )
BASE_ENV = dict(
    os.environ,
    **settings,
    CRM_ENV_FILE=str(ENV_FILE),
    COMPOSE_FILE="compose.yaml:compose.ci.yaml",
    CRM_BIND_ADDRESS="127.0.0.1",
    CRM_IMAGE="crm-ovi:compose-ci",
)
RUN_ID = os.environ["GITHUB_RUN_ID"] + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")
SOURCE = "ovi-ci-" + RUN_ID + "-source"
TARGET = "ovi-ci-" + RUN_ID + "-restore"


def env(project):
    port = "18080" if project == SOURCE else "18081"
    return dict(
        BASE_ENV,
        COMPOSE_PROJECT_NAME=project,
        CRM_HTTP_PORT=port,
        CRM_HTTPS_PORT="18443" if project == SOURCE else "18444",
    )


def compose(project, *args, capture=False):
    result = subprocess.run(
        ["docker", "compose", *args],
        env=env(project),
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else None


def app(project, command):
    return compose(
        project,
        "exec",
        "-T",
        "web",
        "python",
        "scripts/ci/app_probe.py",
        command,
        capture=True,
    )


def request(url, cookie=None):
    headers = {"Cookie": "sessionid=" + cookie} if cookie else {}
    req = urllib.request.Request(url, headers=headers)
    # Explicit local proxy check; never route localhost through an outbound proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


def proxy_checks(project):
    base = "http://127.0.0.1:" + env(project)["CRM_HTTP_PORT"]
    status, body, _ = request(base + "/health/")
    assert status == 200 and body == b"ok", "Caddy -> Gunicorn health failed"
    status, body, headers = request(base + "/login/")
    assert status == 200 and "Вход в систему" in body.decode()
    assert headers["X-Content-Type-Options"] == "nosniff"
    for filename in ["app.css", "htmx.min.js", "alpine.min.js"]:
        status, body, _ = request(base + "/static/" + filename)
        expected = (ROOT / "static" / filename).read_bytes()
        assert (
            status == 200
            and hashlib.sha256(body).digest() == hashlib.sha256(expected).digest()
        ), filename
    for path in [
        "/media/private.txt",
        "/private_media/private.txt",
        "/.env",
        "/.git/config",
    ]:
        assert request(base + path)[0] == 404, path
    auth = json.loads(app(project, "auth"))
    status, body, _ = request(base + "/", auth["session"])
    assert status == 200 and "Главная" in body.decode()
    path = f"/documents/{auth['document']}/download/"
    # Anonymous must never receive the file; opener follows redirect to login.
    status, body, _ = request(base + path)
    assert hashlib.sha256(body).hexdigest() != auth["sha256"]
    status, body, headers = request(base + path, auth["session"])
    assert status == 200 and hashlib.sha256(body).hexdigest() == auth["sha256"]
    assert headers["Content-Disposition"].startswith("attachment")
    assert request(base + "/private_media/" + auth["file"])[0] == 404
    # Use line-delimited JSON from compose ps, supporting array output too.
    raw = compose(project, "ps", "--format", "json", capture=True)
    containers = (
        json.loads(raw)
        if raw.startswith("[")
        else [json.loads(line) for line in raw.splitlines()]
    )
    assert {c["Service"] for c in containers} == {
        "db",
        "redis",
        "web",
        "worker",
        "beat",
        "caddy",
    }
    for c in containers:
        assert c["State"] == "running", c
        if c["Service"] in ("web", "db", "redis"):
            assert c["Health"] == "healthy", c
    print(
        "PASS",
        project,
        "six running services, healthchecks, HTTP proxy, static bytes, protected download",
    )


def main():
    # Refuse accidental reuse; names are unique per workflow run and attempt.
    for project in (SOURCE, TARGET):
        assert not compose(project, "ps", "-aq", capture=True), (
            "CI project already exists"
        )
    compose(SOURCE, "config", "--quiet")
    compose(SOURCE, "build", "web")
    compose(SOURCE, "up", "-d", "--wait", "--wait-timeout", "120", "db", "redis")
    compose(SOURCE, "run", "--rm", "web", "python", "manage.py", "migrate", "--noinput")
    compose(
        SOURCE,
        "run",
        "--rm",
        "web",
        "python",
        "manage.py",
        "makemigrations",
        "--check",
        "--dry-run",
    )
    compose(SOURCE, "run", "--rm", "web", "python", "manage.py", "bootstrap")
    compose(SOURCE, "up", "-d", "--no-build", "--wait", "--wait-timeout", "180")
    print(app(SOURCE, "seed"))
    proxy_checks(SOURCE)
    if os.getenv("CRM_BROWSER_SMOKE") == "1":
        fixture_file = WORK / "browser-fixtures.json"
        fixture_file.write_text(app(SOURCE, "browser-fixtures"))
        fixture_file.chmod(0o600)
        subprocess.run(
            ["node", "scripts/ci/task0011_browser.cjs"],
            check=True,
            env=dict(
                os.environ,
                CRM_BROWSER_FIXTURES=str(fixture_file),
                CRM_BROWSER_URL="http://127.0.0.1:18080",
            ),
        )
    expected = app(SOURCE, "manifest")
    compose(SOURCE, "stop", "web", "worker", "beat")
    subprocess.run(
        ["scripts/backup.sh", str(WORK / "backup")], env=env(SOURCE), check=True
    )
    for name in ("database.dump", "private-media.tar.gz"):
        assert (WORK / "backup" / name).stat().st_size > 0
    # Restore into a different fresh database AND media volume; source is untouched.
    compose(TARGET, "up", "-d", "--wait", "--wait-timeout", "120", "db", "redis")
    subprocess.run(
        ["scripts/restore.sh", str(WORK / "backup")],
        env=dict(env(TARGET), CONFIRM_RESTORE="YES"),
        check=True,
    )
    compose(TARGET, "up", "-d", "--no-build", "--wait", "--wait-timeout", "180")
    assert app(TARGET, "manifest") == expected, (
        "Media file list or SHA-256 differs after restore"
    )
    proxy_checks(TARGET)
    compose(SOURCE, "up", "-d", "--no-build", "--wait", "--wait-timeout", "180")
    assert app(SOURCE, "manifest") == expected, "Source files changed during restore"
    proxy_checks(SOURCE)
    print(
        "PASS PostgreSQL + media backup/restore into separate project; source preserved"
    )


try:
    main()
finally:
    for project in (SOURCE, TARGET):
        for args, suffix in [
            (("ps", "-a"), "status"),
            (("logs", "--no-color", "--tail", "150"), "logs"),
        ]:
            with (WORK / f"{project}-{suffix}.log").open("w") as out:
                subprocess.run(
                    ["docker", "compose", *args],
                    env=env(project),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
    # Deliberately no down -v, rm, system prune, builder prune or volume removal.
    # Ephemeral runner teardown owns cleanup; archives/env/session credentials are not uploaded.
