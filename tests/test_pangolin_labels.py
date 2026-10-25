import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import pangolin_labels as pl

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "project"


# --- Label-Erzeugung ------------------------------------------------------- #
def test_flatten_dict_und_liste():
    out: list[str] = []
    pl.flatten("p", {"a": 1, "t": [{"port": 80}, {"port": 81}], "b": True}, out)
    assert out == ["p.a=1", "p.t[0].port=80", "p.t[1].port=81", "p.b=true"]


def test_build_labels_praefix():
    labels = pl.build_labels("web", {"name": "Web", "mode": "http"})
    assert labels == [
        "pangolin.public-resources.web.name=Web",
        "pangolin.public-resources.web.mode=http",
    ]


# --- Compose / Ports / Alias ---------------------------------------------- #
@pytest.mark.parametrize(
    "entry,expected",
    [("8080:80", 80), ("80", 80), ("8080:80/tcp", 80), ({"target": 8080}, 8080), ("8000-8010", None), ("${PORT}", None)],
)
def test_parse_container_port(entry, expected):
    assert pl.parse_container_port(entry) == expected


def write_compose(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_services_alias_und_healthcheck_port(tmp_path):
    f = write_compose(tmp_path / "compose.yml", """
services:
  nginx:
    image: nginx
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://localhost:8080/_health"]
    networks:
      newt-net:
        aliases: ["${COMPOSE_PROJECT_NAME}-web"]
      internal:
  php:
    image: php
    expose: ["9000"]
    labels:
      - pangolin.public-resources.x.name=X
""")
    by_name = {s.name: s for s in pl.load_services(f)}
    assert by_name["nginx"].ports == [8080]
    assert by_name["nginx"].alias == "${COMPOSE_PROJECT_NAME}-web"
    assert by_name["php"].ports == [9000]
    assert by_name["php"].has_pangolin_labels and not by_name["nginx"].has_pangolin_labels


# --- .env / COMPOSE_FILE --------------------------------------------------- #
def test_read_env_file(tmp_path):
    env = write_compose(tmp_path / ".env", "# Kommentar\nexport A=1\nB='zwei'\nC=\"drei\"\nohne_gleich\n")
    assert pl.read_env_file(env) == {"A": "1", "B": "zwei", "C": "drei"}
    assert pl.read_env_file(tmp_path / "fehlt") == {}


def test_load_project_beispiel():
    services, custom, files = pl.load_project(EXAMPLE)
    assert custom.name == "custom.yml"
    assert [f.name for f in files] == ["compose.yml", "compose.web.yml", "custom.yml"]
    assert all(s.compose_file == custom for s in services)


def test_compose_file_aus_site_config(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    write_compose(tmp_path / "compose.yml", "services: {a: {image: x}}\n")
    write_compose(tmp_path / "custom.yml", "services: {}\n")
    write_compose(tmp_path / "site.config.yml", 'compose:\n  file: "compose.yml:custom.yml"\n')
    files = pl.resolve_compose_files(tmp_path)  # kein .env: Doppelpunkt als Trenner
    assert [f.name for f in files] == ["compose.yml", "custom.yml"]


# --- Validierung ------------------------------------------------------------ #
def good_http(**over):
    res = {"name": "A", "mode": "http", "full-domain": "a.example.com",
           "targets": [{"hostname": "h", "method": "http", "port": 80}]}
    res.update(over)
    return res


def validate(key, res, domains=(), keys=None, doms=None):
    return pl.validate_resource(key, res, list(domains), keys if keys is not None else set(), doms if doms is not None else set())


def test_validate_ok():
    errors, warnings = validate("a", good_http(), [pl.Domain(base="example.com")])
    assert errors == [] and warnings == []


def test_validate_domain_unbekannt_warnt():
    errors, warnings = validate("a", good_http(), [pl.Domain(base="andere.de")])
    assert errors == [] and len(warnings) == 1


def test_validate_http_pflichtfelder():
    res = good_http()
    del res["full-domain"]
    res["targets"][0].pop("method")
    errors, _ = validate("a", res)
    assert any("full-domain" in e for e in errors) and any("method" in e for e in errors)


def test_validate_doppelte_keys_und_domains():
    keys: set[str] = set()
    doms: set[str] = set()
    validate("a", good_http(), keys=keys, doms=doms)
    errors, _ = validate("a", good_http(), keys=keys, doms=doms)
    assert any("Key doppelt" in e for e in errors) and any("full-domain" in e and "doppelt" in e for e in errors)


def test_validate_tcp_regeln():
    res = {"name": "T", "mode": "tcp", "targets": [{"hostname": "h", "port": 1, "method": "http"}], "auth": {"sso-enabled": True}}
    errors, _ = validate("t", res)
    assert any("proxy-port" in e for e in errors)
    assert any("auth" in e for e in errors)
    assert any("method" in e for e in errors)


# --- --spec ----------------------------------------------------------------- #
SPEC = """
services:
  nginx:
    site-nginx:
      name: Site
      mode: http
      full-domain: site.example.com
      targets:
        - {hostname: "${COMPOSE_PROJECT_NAME}-web", method: http, port: 8080}
"""


def test_run_spec_schreibt_idempotent_und_check(tmp_path, capsys):
    spec = write_compose(tmp_path / "spec.yml", SPEC)
    out = tmp_path / "compose.pangolin.yml"
    domains = [pl.Domain(base="example.com")]

    assert pl.run_spec(spec, out, check=True, domains=domains) == 1  # fehlt noch
    assert pl.run_spec(spec, out, check=False, domains=domains) == 0
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# AUTO-GENERATED")
    assert "pangolin.public-resources.site-nginx.full-domain=site.example.com" in text
    assert "targets[0].hostname=${COMPOSE_PROJECT_NAME}-web" in text
    assert pl.run_spec(spec, out, check=True, domains=domains) == 0

    out.write_text(text + "# manipuliert\n", encoding="utf-8")
    assert pl.run_spec(spec, out, check=True, domains=domains) == 1
    assert pl.run_spec(spec, out, check=False, domains=domains) == 0  # stellt Soll wieder her
    assert out.read_text(encoding="utf-8") == text


def test_run_spec_ungueltig_schreibt_nichts(tmp_path):
    spec = write_compose(tmp_path / "spec.yml", SPEC.replace("port: 8080", "port: 0"))
    out = tmp_path / "compose.pangolin.yml"
    assert pl.run_spec(spec, out, check=False, domains=[]) == 2
    assert not out.exists()


# --- write_custom ----------------------------------------------------------- #
def test_write_custom_erhaelt_kommentare_und_fremde_labels(tmp_path):
    pytest.importorskip("ruamel.yaml")
    custom = tmp_path / "custom.yml"
    shutil.copy(EXAMPLE / "custom.yml", custom)
    new = pl.Result(custom, "app", key="app", labels=["pangolin.public-resources.app.name=Neu"])
    pl.write_custom(custom, [new])
    text = custom.read_text(encoding="utf-8")
    assert "Projektspezifische Anpassungen" in text and "Zeitzone" in text  # Kommentare bleiben
    assert "com.example.team=web" in text  # fremdes Label bleibt
    assert "app.name=Neu" in text and "app.name=Old" not in text  # gleicher Key wird ersetzt
    assert (tmp_path / "custom.yml.bak").is_file()


# --- Config ------------------------------------------------------------------ #
def test_domains_from_config_und_api_url():
    cfg = {"domains": ["a.de", {"base": "b.de", "type": "cname", "org": "x"}]}
    doms = pl.domains_from_config(cfg)
    assert [(d.base, d.type, d.org) for d in doms] == [("a.de", "ns", ""), ("b.de", "cname", "x")]
    assert pl.api_url("https://api.example.com", "/orgs") == "https://api.example.com/v1/orgs"
    assert pl.api_url("https://api.example.com/v1/", "/orgs") == "https://api.example.com/v1/orgs"


# --- Import (API -> Spec), mit erfundenen Antworten ------------------------------- #
DETAIL = {"name": "Demo", "mode": "http", "fullDomain": "demo.example.com", "sso": True, "forwardIdentityHeaders": False,
          "applyRules": True, "enabled": True, "requestHeaders": '[{"name": "X-A", "value": "1"}]'}
TARGET = {"ip": "old-host", "port": 80, "method": "http", "enabled": True, "hcEnabled": True, "hcHostname": "old-host", "hcPort": 80,
          "hcPath": "/", "hcScheme": "http", "hcInterval": 5, "hcHeaders": None, "path": None}
ROLES = [{"name": "Admin", "isAdmin": True}, {"name": "Member", "isAdmin": False}]


def test_import_uebernimmt_auth_und_laesst_admin_rolle_weg():
    res, notes = pl.build_import_resource(DETAIL, [TARGET], ROLES, [], [], keep_healthcheck=True)
    assert res["full-domain"] == "demo.example.com"
    assert res["auth"] == {"sso-enabled": True, "sso-roles": ["Member"]}
    assert res["forward-identity-headers"] is False
    assert res["request-headers"] == [{"name": "X-A", "value": "1"}]
    assert "site" not in res["targets"][0]
    assert any("Regeln" in n for n in notes)  # nicht uebernommen -> Hinweis


def test_import_healthcheck_keep_oder_disable():
    keep, _ = pl.build_import_resource(DETAIL, [TARGET], ROLES, [], [], keep_healthcheck=True)
    off, _ = pl.build_import_resource(DETAIL, [TARGET], ROLES, [], [], keep_healthcheck=False)
    assert keep["targets"][0]["healthcheck"]["enabled"] is True
    assert off["targets"][0]["healthcheck"]["enabled"] is False
    assert off["targets"][0]["healthcheck"]["path"] == "/"  # Werte bleiben erhalten
    no_hc, _ = pl.build_import_resource(DETAIL, [{**TARGET, "hcEnabled": False}], ROLES, [], [], keep_healthcheck=True)
    assert "healthcheck" not in no_hc["targets"][0]


def test_import_hostname_ersetzen_auch_im_healthcheck():
    res, _ = pl.build_import_resource(DETAIL, [TARGET], ROLES, [], [], keep_healthcheck=True, hostname="${COMPOSE_PROJECT_NAME}-web")
    assert res["targets"][0]["hostname"] == "${COMPOSE_PROJECT_NAME}-web"
    assert res["targets"][0]["healthcheck"]["hostname"] == "${COMPOSE_PROJECT_NAME}-web"


def test_import_tcp_ohne_method():
    res, _ = pl.build_import_resource({"name": "T", "mode": "tcp", "proxyPort": 1883}, [{"ip": "mqtt", "port": 1883}], [], [], [], True)
    assert res["proxy-port"] == 1883 and "method" not in res["targets"][0] and "full-domain" not in res


def test_import_ergebnis_besteht_die_validierung():
    res, _ = pl.build_import_resource(DETAIL, [TARGET], ROLES, [], [], keep_healthcheck=False)
    errors, _ = pl.validate_resource("demo", res, [], set(), set())
    assert errors == []


def test_run_resources_import_schreibt_spec(tmp_path, monkeypatch):
    summaries = {"data": {"resources": [{"resourceId": 7, "niceId": "demo-nice", "passwordId": 3, "pincodeId": None, "headerAuthId": None}]}}

    def fake_api_get(endpoint, key, path):
        if path == "/org/o/resources":
            return summaries
        data = {"/resource/7": DETAIL, "/resource/7/targets": {"targets": [TARGET]}, "/resource/7/roles": {"roles": ROLES}}
        return {"data": data.get(path, {})}

    monkeypatch.setattr(pl, "api_get", fake_api_get)
    monkeypatch.setenv("PANGOLIN_API_KEY", "x.y")
    out = tmp_path / "spec.yml"

    args = SimpleNamespace(org="o", list_resources=False, import_id="demo-nice", service="web", hostname=None,
                           healthcheck="disable", out=str(out))

    assert pl.run_resources(args, {"endpoint": "https://api.example.com"}) == 0
    spec_text = out.read_text(encoding="utf-8")
    assert "demo-nice:" in spec_text and "enabled: false" in spec_text
    assert pl.run_spec(out, tmp_path / "o.yml", check=False, domains=[]) == 0  # Import-Ausgabe ist eine gueltige Spec


# --- Status (Labels gegen Resources) ------------------------------------------------ #
def test_collect_label_resources(tmp_path):
    f = write_compose(tmp_path / "compose.pangolin.yml", """
services:
  nginx:
    labels:
      - pangolin.public-resources.site-web.name=Web
      - pangolin.public-resources.site-web.full-domain=site.example.com
      - pangolin.public-resources.site-web.targets[0].hostname=${COMPOSE_PROJECT_NAME}-web
      - com.example.other=1
  pma:
    labels:
      pangolin.public-resources.site-pma.full-domain: db.${DOMAIN}
""")
    found = pl.collect_label_resources([f])
    assert found["site-web"]["domain"] == "site.example.com" and found["site-web"]["service"] == "nginx"
    assert found["site-pma"]["domain"] == ""  # Variable: Domain unbekannt
    assert len(found) == 2


def test_classify_status_vier_faelle():
    resources = [
        {"niceId": "site-web", "fullDomain": "site.example.com"},      # Key == niceId -> ok
        {"niceId": "wild-fox", "fullDomain": "db.example.com"},        # gleiche Domain, anderer niceId -> umbenennen
        {"niceId": "lonely-owl", "fullDomain": "x.example.com"},       # kein Label -> offen
    ]
    labels = {
        "site-web": {"domain": "site.example.com", "file": "a.yml", "service": "nginx"},
        "site-pma": {"domain": "db.example.com", "file": "a.yml", "service": "pma"},
        "new-one": {"domain": "new.example.com", "file": "b.yml", "service": "app"},   # keine Resource -> nur Label
    }
    status = pl.classify_status(resources, labels)
    assert [k for k, _ in status["ok"]] == ["site-web"]
    assert [(old["niceId"], k) for k, old in status["rename"]] == [("wild-fox", "site-pma")]
    assert [k for k, _ in status["label_only"]] == ["new-one"]
    assert [r["niceId"] for r in status["open"]] == ["lonely-owl"]


def test_run_status_ausgabe(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PANGOLIN_API_KEY", "x.y")
    monkeypatch.setattr(pl, "fetch_resource_summaries", lambda *a: [{"niceId": "wild-fox", "fullDomain": "site.example.com", "name": "W"}])
    f = write_compose(tmp_path / "c.yml", "services:\n  a:\n    labels:\n      - pangolin.public-resources.site-web.full-domain=site.example.com\n")
    args = SimpleNamespace(org="o", compose=[str(f)], project=None)
    assert pl.run_status(args, {"endpoint": "https://api.example.com"}) == 0
    out = capsys.readouterr().out
    assert "wild-fox  ->  site-web" in out and "[offen] 0" in out
