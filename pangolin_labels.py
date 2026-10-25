#!/usr/bin/env python3
"""pangolin-labels: gefuehrter Generator fuer Pangolin-Blueprint-Labels.

Liest eine oder mehrere docker-compose-Dateien, fragt pro Service interaktiv
Domain, Auth usw. ab und gibt die passenden `pangolin.public-resources.*`-Labels
aus (auf Wunsch als Compose-Override-Datei).

Domains kommen aus der Pangolin Integration-API (GET /org/{orgId}/domains) oder
als Fallback aus der Konfigurationsdatei.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

LABEL_PREFIX = "pangolin.public-resources"
CONFIG_SEARCH = [
    Path("pangolin-labels.yaml"),
    Path.home() / ".config" / "pangolin-labels" / "config.yaml",
    Path("/etc/pangolin-labels/config.yaml"),
]
# Images, die wir selbst nie veroeffentlichen wollen (Newt / Pangolin-Site-Connector)
SKIP_IMAGE_HINTS = ("fosrl/",)


# --------------------------------------------------------------------------- #
# Datenmodelle
# --------------------------------------------------------------------------- #
@dataclass
class Domain:
    base: str
    type: str = "ns"  # ns | cname | wildcard (laut Pangolin-API)
    verified: bool = True
    source: str = "config"
    org: str = ""

    def label(self) -> str:
        extra = []
        if self.org:
            extra.append(f"Org: {self.org}")
        if self.type == "cname":
            extra.append("cname: nur exakt diese Domain")
        if not self.verified:
            extra.append("nicht verifiziert")
        return self.base + (f"  ({', '.join(extra)})" if extra else "")


@dataclass
class ServiceInfo:
    name: str
    image: str
    ports: list[int]
    has_pangolin_labels: bool
    compose_file: Path
    alias: str = ""  # Netzwerk-Alias, unter dem der Connector den Container erreicht


@dataclass
class Result:
    compose_file: Path
    service: str
    key: str = ""
    labels: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Konfiguration + Domains
# --------------------------------------------------------------------------- #
def load_config(explicit: str | None) -> dict:
    candidates = [Path(explicit)] if explicit else CONFIG_SEARCH
    for path in candidates:
        if path.is_file():
            with path.open(encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            cfg["_path"] = str(path)
            return cfg
    if explicit:
        sys.exit(f"Konfigurationsdatei nicht gefunden: {explicit}")
    return {}


def domains_from_config(cfg: dict) -> list[Domain]:
    out: list[Domain] = []
    for item in cfg.get("domains", []) or []:
        if isinstance(item, str):
            out.append(Domain(base=item))
        elif isinstance(item, dict) and item.get("base"):
            out.append(
                Domain(
                    base=item["base"],
                    type=item.get("type", "ns"),
                    verified=item.get("verified", True),
                    org=str(item.get("org", "")),
                )
            )
    return out


def api_url(endpoint: str, path: str) -> str:
    base = endpoint.rstrip("/")
    if not re.search(r"/v\d+$", base):
        base += "/v1"
    return base + path


def api_get(endpoint: str, api_key: str, path: str) -> dict:
    url = api_url(endpoint, path)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"API antwortete mit HTTP {exc.code} ({url})") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"API nicht erreichbar oder ungueltige Antwort: {exc}") from exc


def configured_orgs(cfg: dict) -> list[str]:
    """org_id (String), org_id/orgs (Liste) aus der Konfiguration; leer = alle per API ermitteln."""
    raw = cfg.get("orgs", cfg.get("org_id"))
    if not raw:
        return []
    return [str(o) for o in raw] if isinstance(raw, list) else [str(raw)]


def domains_from_api(cfg: dict) -> list[Domain]:
    endpoint = cfg.get("endpoint")
    key_env = cfg.get("api_key_env", "PANGOLIN_API_KEY")
    api_key = os.environ.get(key_env) or cfg.get("api_key")
    if not (endpoint and api_key):
        raise RuntimeError(f"endpoint oder API-Key fehlt (Umgebungsvariable {key_env})")
    orgs = configured_orgs(cfg)
    if not orgs:  # keine Org konfiguriert: alle Orgs des API-Keys verwenden
        payload = api_get(endpoint, api_key, "/orgs")
        orgs = [o["orgId"] for o in (payload.get("data") or {}).get("orgs") or [] if o.get("orgId")]
    out: list[Domain] = []
    for org_id in orgs:
        payload = api_get(endpoint, api_key, f"/org/{org_id}/domains?limit=1000&offset=0")
        for d in (payload.get("data") or {}).get("domains") or []:
            if d.get("baseDomain"):
                out.append(
                    Domain(
                        base=d["baseDomain"],
                        type=d.get("type", "ns"),
                        verified=bool(d.get("verified", True)),
                        source="api",
                        org=org_id,
                    )
                )
    return out


def api_credentials(cfg: dict) -> tuple[str, str]:
    endpoint = cfg.get("endpoint")
    key_env = cfg.get("api_key_env", "PANGOLIN_API_KEY")
    api_key = os.environ.get(key_env) or cfg.get("api_key")
    if not (endpoint and api_key):
        sys.exit(f"endpoint oder API-Key fehlt (Umgebungsvariable {key_env})")
    return endpoint, api_key


def get_domains(cfg: dict, use_api: bool) -> list[Domain]:
    if use_api and cfg.get("endpoint"):
        try:
            domains = domains_from_api(cfg)
            print(f"[info] {len(domains)} Domain(s) aus der Pangolin-API geladen.")
            return domains
        except RuntimeError as exc:
            print(f"[warnung] Domains aus API nicht ladbar: {exc}", file=sys.stderr)
            print("[warnung] Fallback auf Domains aus der Konfigurationsdatei.", file=sys.stderr)
    return domains_from_config(cfg)


# --------------------------------------------------------------------------- #
# Compose einlesen
# --------------------------------------------------------------------------- #
def parse_container_port(entry) -> int | None:
    """Container-Port aus einem `ports:`/`expose:`-Eintrag ermitteln."""
    if isinstance(entry, dict):
        target = entry.get("target")
        return int(target) if str(target).isdigit() else None
    text = str(entry).split("/")[0]  # Protokoll abschneiden (8080:80/tcp)
    last = text.split(":")[-1]
    # Portbereiche ("8000-8010") ignorieren, Variablen (${PORT}) ebenfalls
    return int(last) if last.isdigit() else None


def load_services(path: Path) -> list[ServiceInfo]:
    with path.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    services = doc.get("services") or {}
    result: list[ServiceInfo] = []
    for name, spec in services.items():
        spec = spec or {}
        ports: list[int] = []
        for entry in list(spec.get("expose", []) or []) + list(spec.get("ports", []) or []):
            port = parse_container_port(entry)
            if port and port not in ports:
                ports.append(port)
        if not ports:  # nicht exponiert: Port aus dem Healthcheck (http://localhost:PORT) vorschlagen
            hc = " ".join(map(str, (spec.get("healthcheck") or {}).get("test") or []))
            found = re.search(r"localhost:(\d+)", hc)
            if found:
                ports.append(int(found.group(1)))
        nets = spec.get("networks")
        aliases = [a for n in (nets.values() if isinstance(nets, dict) else []) for a in ((n or {}).get("aliases") or [])]
        labels = spec.get("labels") or []
        label_keys = list(labels.keys()) if isinstance(labels, dict) else [str(x) for x in labels]
        result.append(
            ServiceInfo(
                name=name,
                image=str(spec.get("image", "")),
                ports=ports,
                has_pangolin_labels=any(k.startswith("pangolin.") for k in label_keys),
                compose_file=path,
                alias=str(aliases[0]) if aliases else "",
            )
        )
    return result


def read_env_file(path: Path) -> dict[str, str]:
    """Einfacher .env-Parser (KEY=VALUE, Kommentare, optionale Anfuehrungszeichen)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def read_site_config(project: Path) -> dict:
    """site.config.yml des Websites-Frameworks (die .env daraus ist generiert und nicht ueberall vorhanden)."""
    path = project / "site.config.yml"
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def resolve_compose_files(project: Path) -> list[Path]:
    """Compose-Dateien in der Reihenfolge aus COMPOSE_FILE (.env, Umgebung, sonst site.config.yml)."""
    env = read_env_file(project / ".env")
    raw = env.get("COMPOSE_FILE") or os.environ.get("COMPOSE_FILE") or (read_site_config(project).get("compose") or {}).get("file") or ""
    if not raw:
        sys.exit(f"COMPOSE_FILE weder in {project / '.env'} noch in site.config.yml gefunden.")
    sep = env.get("COMPOSE_PATH_SEPARATOR") or os.environ.get("COMPOSE_PATH_SEPARATOR")
    if not sep:
        sep = "," if "," in raw else ":"
    files = [project / part.strip() for part in raw.split(sep) if part.strip()]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        sys.exit("Compose-Datei(en) aus COMPOSE_FILE nicht gefunden: " + ", ".join(missing))
    return files


def load_project(project: Path) -> tuple[list[ServiceInfo], Path, list[Path]]:
    """Alle Dateien aus COMPOSE_FILE zu einer Service-Sicht zusammenfuehren.

    Ziel fuer neue Labels ist die custom.yml (auch wenn sie noch nicht existiert).
    """
    files = resolve_compose_files(project)
    merged: dict[str, ServiceInfo] = {}
    for path in files:
        for svc in load_services(path):
            known = merged.get(svc.name)
            if known is None:
                merged[svc.name] = svc
                continue
            for port in svc.ports:
                if port not in known.ports:
                    known.ports.append(port)
            known.image = svc.image or known.image
            known.alias = known.alias or svc.alias
            known.has_pangolin_labels = known.has_pangolin_labels or svc.has_pangolin_labels
    custom = next((f for f in files if f.name in ("custom.yml", "custom.yaml")), project / "custom.yml")
    for svc in merged.values():
        svc.compose_file = custom
    return list(merged.values()), custom, files


# --------------------------------------------------------------------------- #
# Eingabe-Helfer
# --------------------------------------------------------------------------- #
def ask(text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        answer = input(f"  {text}{suffix}: ").strip()
    except EOFError:
        sys.exit("\nEingabe beendet - Abbruch.")
    return answer or (default or "")


def ask_yes_no(text: str, default: bool = False) -> bool:
    hint = "J/n" if default else "j/N"
    while True:
        answer = ask(f"{text} ({hint})").lower()
        if not answer:
            return default
        if answer in ("j", "ja", "y", "yes"):
            return True
        if answer in ("n", "nein", "no"):
            return False
        print("    Bitte mit j oder n antworten.")


def choose(text: str, options: list[str], default: int = 1) -> int:
    """Gibt den 1-basierten Index der Auswahl zurueck."""
    for idx, opt in enumerate(options, 1):
        print(f"    {idx}) {opt}")
    while True:
        answer = ask(text, str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        print(f"    Bitte eine Zahl von 1 bis {len(options)} eingeben.")


def split_list(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-") or "resource"


def env_ref(key: str, suffix: str) -> str:
    var = re.sub(r"[^A-Z0-9]+", "_", f"PANGOLIN_{key}_{suffix}".upper()).strip("_")
    return "${" + var + "}"


# --------------------------------------------------------------------------- #
# Label-Erzeugung
# --------------------------------------------------------------------------- #
def fmt_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def flatten(prefix: str, value, out: list[str]) -> None:
    if isinstance(value, dict):
        for key, val in value.items():
            flatten(f"{prefix}.{key}", val, out)
    elif isinstance(value, list):
        for idx, val in enumerate(value):
            flatten(f"{prefix}[{idx}]", val, out)
    else:
        out.append(f"{prefix}={fmt_value(value)}")


def build_labels(key: str, resource: dict) -> list[str]:
    out: list[str] = []
    flatten(f"{LABEL_PREFIX}.{key}", resource, out)
    return out


def pick_site(cfg: dict, domain: Domain | None) -> str | None:
    """Site per Rueckfrage waehlen; Vorschlag anhand der gewaehlten Domain."""
    sites = cfg.get("sites") or {}
    # Sites mit `org:` nur anbieten, wenn sie zur Org der gewaehlten Domain passen
    if domain and domain.org:
        sites = {
            n: s for n, s in sites.items()
            if not (s or {}).get("org") or str(s["org"]) == domain.org
        }
    if not sites:
        return None
    names = list(sites.keys())
    suggestion = None
    if domain:
        matches = [
            n for n, s in sites.items()
            if domain.base in ((s or {}).get("domains") or [])
        ]
        if len(matches) == 1:
            suggestion = matches[0]
    default_idx = names.index(suggestion) + 1 if suggestion else len(names) + 1
    options = [
        f"{n}" + (f"  (Domains: {', '.join((s or {}).get('domains', []))})" if (s or {}).get("domains") else "")
        for n, s in sites.items()
    ] + ["(keine - Site des Newt-Connectors verwenden)"]
    if suggestion:
        print(f"    Vorschlag anhand der Domain: {suggestion}")
    idx = choose("Site", options, default_idx)
    if idx > len(names):
        return None
    return str((sites[names[idx - 1]] or {}).get("id") or names[idx - 1])


def ask_auth(key: str, secrets_note: list[str]) -> dict:
    print("  Authentifizierung:")
    methods = ["Pangolin-SSO", "PIN-Code", "Passwort", "HTTP Basic Auth"]
    for idx, name in enumerate(methods, 1):
        print(f"    {idx}) {name}")
    raw = ask("Methoden (Nummern, kommagetrennt; Enter = keine)", "")
    picked = {p for p in split_list(raw) if p.isdigit() and 1 <= int(p) <= len(methods)}
    auth: dict = {}
    if "1" in picked:
        auth["sso-enabled"] = True
        roles = split_list(ask("SSO-Rollen (kommagetrennt, leer = keine)", ""))
        users = split_list(ask("SSO-Benutzer/E-Mails (kommagetrennt, leer = keine)", ""))
        if roles:
            auth["sso-roles"] = roles
        if users:
            auth["sso-users"] = users
    if "2" in picked:
        default = env_ref(key, "PIN")
        auth["pincode"] = ask("PIN (6 Ziffern oder ${VARIABLE})", default)
        if auth["pincode"] == default:
            secrets_note.append(default)
    if "3" in picked:
        default = env_ref(key, "PASSWORD")
        auth["password"] = ask("Passwort (oder ${VARIABLE})", default)
        if auth["password"] == default:
            secrets_note.append(default)
    if "4" in picked:
        default = env_ref(key, "BASIC_PASSWORD")
        auth["basic-auth"] = {
            "user": ask("Basic-Auth-Benutzer", "admin"),
            "password": ask("Basic-Auth-Passwort (oder ${VARIABLE})", default),
        }
        if auth["basic-auth"]["password"] == default:
            secrets_note.append(default)
    return auth


def ask_domain(service: str, domains: list[Domain], used: set[str], site_domain: str = "") -> tuple[str, Domain | None]:
    # Vorauswahl: Domain, zu der die Domain der Site (site.config.yml) gehoert
    fits = [i for i, d in enumerate(domains, 1) if site_domain == d.base or site_domain.endswith("." + d.base)]
    default_idx = max(fits, key=lambda i: len(domains[i - 1].base)) if fits else 1
    while True:
        chosen: Domain | None = None
        if domains:
            idx = choose("Domain", [d.label() for d in domains], default_idx)
            chosen = domains[idx - 1]
            if chosen.type == "cname":
                full = chosen.base
                print(f"    -> {full}")
            else:
                sub = ask("Subdomain ('.' = Hauptdomain)", slug(service))
                full = f"{sub}.{chosen.base}" if sub not in ("", ".") else chosen.base
        else:
            full = ask("Vollstaendiger Domainname (z. B. app.example.com)")
        if not full:
            print("    Ein Domainname ist erforderlich.")
        elif full in used:
            print(f"    {full} wird bereits verwendet - bitte eine andere waehlen.")
        else:
            used.add(full)
            return full, chosen


def ask_advanced(resource: dict, has_sso: bool) -> None:
    if not ask_yes_no("Erweiterte Optionen", False):
        return
    if has_sso and not ask_yes_no("Identitaets-Header (Remote-User, ...) an das Backend weitergeben", True):
        resource["forward-identity-headers"] = False
    hc_path = ask("Healthcheck-Pfad (z. B. /health, leer = keiner)", "")
    if hc_path:
        resource["_healthcheck_path"] = hc_path
    headers = []
    print("  Zusaetzliche Request-Header (Format Name=Wert, Leerzeile beendet):")
    while True:
        line = ask("Header", "")
        if not line:
            break
        if "=" not in line:
            print("    Format: Name=Wert")
            continue
        name, value = line.split("=", 1)
        headers.append({"name": name.strip(), "value": value.strip()})
    if headers:
        resource["request-headers"] = headers
    res_labels = split_list(ask("Pangolin-Resource-Labels (kommagetrennt, leer = keine)", ""))
    if res_labels:
        resource["labels"] = res_labels


# --------------------------------------------------------------------------- #
# Pro Service abfragen
# --------------------------------------------------------------------------- #
def configure_service(
    svc: ServiceInfo,
    cfg: dict,
    domains: list[Domain],
    used_keys: set[str],
    used_domains: set[str],
    secrets_note: list[str],
) -> tuple[str, list[str]] | None:
    ports_txt = ", ".join(str(p) for p in svc.ports) or "keine erkannt"
    print(f"\n=== {svc.name}  (Image: {svc.image or '-'}, Container-Ports: {ports_txt}) ===")
    if svc.has_pangolin_labels:
        if not ask_yes_no("Hat bereits pangolin.*-Labels. Trotzdem neu konfigurieren", False):
            return None
    if not ask_yes_no("Ueber Pangolin veroeffentlichen", False):
        return None

    mode_idx = choose("Typ", ["http (Website/API)", "tcp", "udp"], 1)
    mode = ["http", "tcp", "udp"][mode_idx - 1]

    key_default = slug(f"{cfg['_key_prefix']}-{svc.name}" if cfg.get("_key_prefix") else svc.name)  # Keys sind pro Org eindeutig
    while True:
        key = slug(ask("Resource-Key (stabile ID im Blueprint)", key_default))
        if key in used_keys:
            print(f"    Key '{key}' ist schon vergeben.")
            continue
        used_keys.add(key)
        break
    name = ask("Anzeigename", svc.name.replace("-", " ").replace("_", " ").title())

    resource: dict = {"name": name, "mode": mode}
    domain_obj: Domain | None = None

    if mode == "http":
        full, domain_obj = ask_domain(svc.name, domains, used_domains, cfg.get("_site_domain", ""))
        resource["full-domain"] = full
    else:
        port_txt = ask("Oeffentlicher Port auf dem Pangolin-Server (proxy-port)")
        while not port_txt.isdigit():
            port_txt = ask("Bitte eine Portnummer angeben")
        resource["proxy-port"] = int(port_txt)

    # Ziel-Port
    if len(svc.ports) > 1:
        idx = choose("Ziel-Port im Container", [str(p) for p in svc.ports], 1)
        port = svc.ports[idx - 1]
    elif len(svc.ports) == 1:
        port = int(ask("Ziel-Port im Container", str(svc.ports[0])))
    else:
        txt = ask("Ziel-Port im Container (nicht erkannt)")
        while not txt.isdigit():
            txt = ask("Bitte eine Portnummer angeben")
        port = int(txt)

    target: dict = {}
    site = pick_site(cfg, domain_obj)
    if site:
        target["site"] = site
    if svc.alias:
        target["hostname"] = svc.alias
    if mode == "http":
        default_method = "https" if port in (443, 8443) else "http"
        method_idx = choose("Protokoll zum Backend", ["http", "https", "h2c"], ["http", "https", "h2c"].index(default_method) + 1)
        target["method"] = ["http", "https", "h2c"][method_idx - 1]
    target["port"] = port
    resource["targets"] = [target]

    if mode == "http":
        auth = ask_auth(key, secrets_note)
        if auth:
            resource["auth"] = auth
        ask_advanced(resource, has_sso=bool(auth.get("sso-enabled")))
        hc_path = resource.pop("_healthcheck_path", None)
        if hc_path:
            target["healthcheck"] = {"hostname": svc.alias or svc.name, "port": port, "path": hc_path}
            # Healthcheck-Eintraege am Target sind gueltig; Reihenfolge der Labels bleibt stabil
    return key, build_labels(key, resource)


# --------------------------------------------------------------------------- #
# Ausgabe
# --------------------------------------------------------------------------- #
def render_override(results: list[Result]) -> str:
    services: dict = {}
    for res in results:
        services[res.service] = {"labels": res.labels}
    return yaml.safe_dump({"services": services}, sort_keys=False, width=10_000, allow_unicode=True)


def render_snippet(res: Result) -> str:
    body = {"labels": res.labels}
    text = yaml.safe_dump(body, sort_keys=False, width=10_000, allow_unicode=True)
    return f"# {res.compose_file.name} -> Service '{res.service}'\n" + text


def validate_resource(key: str, res: dict, domains: list[Domain], seen_keys: set[str], seen_domains: set[str]) -> tuple[list[str], list[str]]:
    """Regeln aus der Pangolin-Doku; gibt (Fehler, Warnungen) zurueck."""
    errors: list[str] = []
    warnings: list[str] = []
    where = f"Resource {key}"
    if key in seen_keys:
        errors.append(f"{where}: Key doppelt")
    seen_keys.add(key)
    mode = res.get("mode", "http")
    targets = res.get("targets") or []
    if not targets:
        errors.append(f"{where}: targets fehlt")
    for idx, tgt in enumerate(targets):
        for field_name in ("hostname", "port"):
            if not tgt.get(field_name):
                errors.append(f"{where}: targets[{idx}].{field_name} fehlt")
    if mode == "http":
        full = res.get("full-domain")
        if not full:
            errors.append(f"{where}: full-domain fehlt (mode http)")
        else:
            if full in seen_domains:
                errors.append(f"{where}: full-domain {full} doppelt")
            seen_domains.add(full)
            if domains and not any(full == d.base or full.endswith("." + d.base) for d in domains):
                warnings.append(f"{where}: {full} gehoert zu keiner bekannten Domain der Org")
        for idx, tgt in enumerate(targets):
            if not tgt.get("method"):
                errors.append(f"{where}: targets[{idx}].method fehlt (mode http)")
    elif mode in ("tcp", "udp"):
        if not res.get("proxy-port"):
            errors.append(f"{where}: proxy-port fehlt (mode {mode})")
        if res.get("auth"):
            errors.append(f"{where}: auth ist bei {mode} nicht erlaubt")
        if any(t.get("method") for t in targets):
            errors.append(f"{where}: method ist bei {mode} nicht erlaubt")
    else:
        errors.append(f"{where}: unbekannter mode {mode}")
    return errors, warnings


def run_spec(spec_path: Path, out: Path, check: bool, domains: list[Domain]) -> int:
    """Nicht-interaktiv: Resources aus der Spec validieren und als Compose-Override schreiben (oder --check)."""
    with spec_path.open(encoding="utf-8") as fh:
        spec = yaml.safe_load(fh) or {}
    results: list[Result] = []
    errors: list[str] = []
    seen_keys: set[str] = set()
    seen_domains: set[str] = set()
    for service, resources in (spec.get("services") or {}).items():
        labels: list[str] = []
        for key, res in (resources or {}).items():
            errs, warns = validate_resource(key, res or {}, domains, seen_keys, seen_domains)
            errors += errs
            for w in warns:
                print(f"[warnung] {w}", file=sys.stderr)
            labels += build_labels(key, res or {})
        results.append(Result(out, service, labels=labels))
    if errors:
        print("Spec ungueltig:\n- " + "\n- ".join(errors), file=sys.stderr)
        return 2
    text = "# AUTO-GENERATED by pangolin-labels --spec - nicht von Hand aendern\n" + render_override(results)
    current = out.read_text(encoding="utf-8") if out.is_file() else None
    if check:
        print(f"{out}: " + ("aktuell" if current == text else "weicht ab"))
        return 0 if current == text else 1
    if current == text:
        print(f"{out}: unveraendert")
    else:
        out.write_text(text, encoding="utf-8")
        print(f"[geschrieben] {out}")
    return 0


# --------------------------------------------------------------------------- #
# Import bestehender Resources (API -> Spec)
# --------------------------------------------------------------------------- #
def _drop_none(data: dict) -> dict:
    return {k: v for k, v in data.items() if v is not None}


def _header_list(raw) -> list[dict] | None:
    """Request-/Response-Header aus der API (Liste oder JSON-Text) als [{name, value}]."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, list):
        return [{"name": str(h["name"]), "value": str(h["value"])} for h in raw if isinstance(h, dict) and "name" in h and "value" in h]
    return None


def build_import_resource(
    res: dict, targets: list[dict], roles: list[dict], users: list[dict], whitelist: list[dict],
    keep_healthcheck: bool, hostname: str | None = None,
) -> tuple[dict, list[str]]:
    """Resource (Detail-Antwort der API) in das Blueprint-Format uebersetzen.

    Gibt (Resource, Hinweise) zurueck; die Hinweise nennen alles, was nicht uebernommen werden konnte.
    Die Site der Targets wird bewusst weggelassen (es gilt die Site des Connectors).
    """
    notes: list[str] = []
    mode = res.get("mode") or "http"
    out: dict = {"name": res.get("name"), "mode": mode}
    if mode == "http":
        out["full-domain"] = res.get("fullDomain")
    elif res.get("proxyPort"):
        out["proxy-port"] = res["proxyPort"]

    if res.get("sso") or whitelist:
        auth: dict = {}
        if res.get("sso"):
            auth["sso-enabled"] = True
            names = [r["name"] for r in roles if not r.get("isAdmin")]  # Admin-Rollen setzt Pangolin selbst
            if names:
                auth["sso-roles"] = names
            # shortcut: Feldname der Benutzer (email/username) ist nicht bestaetigt, bei Bedarf anpassen
            idents = [u.get("email") or u.get("username") for u in users]
            if any(idents):
                auth["sso-users"] = [i for i in idents if i]
        mails = [w.get("email") for w in whitelist if w.get("email")]
        if mails:
            auth["whitelist-users"] = mails
        if auth:
            out["auth"] = auth
    if res.get("forwardIdentityHeaders") is False:
        out["forward-identity-headers"] = False
    if res.get("setHostHeader"):
        out["host-header"] = res["setHostHeader"]
    if res.get("tlsServerName"):
        out["tls-server-name"] = res["tlsServerName"]
    for api_key, label in (("requestHeaders", "request-headers"), ("responseHeaders", "response-headers")):
        if res.get(api_key):
            headers = _header_list(res[api_key])
            if headers:
                out[label] = headers
            else:
                notes.append(f"{label} nicht lesbar (Format unbekannt)")

    out_targets = []
    for tgt in targets:
        host = hostname or tgt.get("ip")
        item: dict = {"hostname": host, "port": tgt.get("port")}
        if mode == "http":
            item["method"] = tgt.get("method") or "http"
        if tgt.get("enabled") is False:
            item["enabled"] = False
        item.update(_drop_none({
            "path": tgt.get("path"), "path-match": tgt.get("pathMatchType"),
            "rewrite-path": tgt.get("rewritePath"), "rewrite-match": tgt.get("rewritePathType"),
        }))
        if tgt.get("hcEnabled"):
            hc_host = tgt.get("hcHostname")
            item["healthcheck"] = _drop_none({
                "hostname": host if hostname and hc_host == tgt.get("ip") else hc_host,
                "port": tgt.get("hcPort"), "enabled": keep_healthcheck, "path": tgt.get("hcPath"),
                "scheme": tgt.get("hcScheme"), "mode": tgt.get("hcMode"), "method": tgt.get("hcMethod"),
                "interval": tgt.get("hcInterval"), "unhealthy-interval": tgt.get("hcUnhealthyInterval"),
                "timeout": tgt.get("hcTimeout"), "follow-redirects": tgt.get("hcFollowRedirects"),
                "status": tgt.get("hcStatus"), "healthy-threshold": tgt.get("hcHealthyThreshold"),
                "unhealthy-threshold": tgt.get("hcUnhealthyThreshold"),
                "headers": _header_list(tgt.get("hcHeaders")),
            })
        out_targets.append(item)
    out["targets"] = out_targets

    # nicht uebernommen
    if res.get("enabled") is False:
        notes.append("Resource ist deaktiviert (wird beim Anwenden vermutlich aktiv)")
    for field_name, text in (
        ("applyRules", "Regeln (rules)"), ("mtlsEnabled", "mTLS"), ("maintenanceModeEnabled", "Wartungsmodus"),
        ("stickySession", "Sticky Session"), ("blockAccess", "Zugriff blockiert"),
    ):
        if res.get(field_name):
            notes.append(f"{text} nicht uebernommen")
    if res.get("loadBalancingStrategy") not in (None, "", "wrr"):
        notes.append(f"Load-Balancing-Strategie {res['loadBalancingStrategy']} nicht uebernommen")
    if len(targets) > 1:
        notes.append(f"{len(targets)} Targets uebernommen (Load-Balancing pruefen)")
    return _drop_none(out), notes


def fetch_resource_summaries(endpoint: str, api_key: str, org: str) -> list[dict]:
    payload = api_get(endpoint, api_key, f"/org/{org}/resources")
    return list((payload.get("data") or {}).get("resources") or [])


def run_resources(args, cfg: dict) -> int:
    """--list-resources / --import: bestehende Resources aus der Pangolin-API lesen."""
    endpoint, api_key = api_credentials(cfg)
    orgs = configured_orgs(cfg)
    org = args.org or (orgs[0] if len(orgs) == 1 else None)
    if not org:
        sys.exit("Org angeben: --org <org-id>")
    try:
        summaries = fetch_resource_summaries(endpoint, api_key, org)
        if args.list_resources:
            print(f"Org {org}: {len(summaries)} Resource(s)")
            for r in sorted(summaries, key=lambda x: ((x.get("sites") or [{}])[0].get("siteNiceId", ""), x.get("niceId", ""))):
                site = ", ".join(s.get("siteNiceId", "?") for s in r.get("sites") or []) or "-"
                state = "an" if r.get("enabled") else "AUS"
                hc = "hc" if any(t.get("hcEnabled") for t in r.get("targets") or []) else "-"
                print(f"- {r.get('niceId')}  {r.get('fullDomain') or r.get('proxyPort')}  Site: {site}  {state}  {hc}  \"{r.get('name')}\"")
            return 0
        match = next((r for r in summaries if args.import_id in (r.get("niceId"), str(r.get("resourceId")))), None)
        if match is None:
            sys.exit(f"Resource {args.import_id} nicht in Org {org} gefunden (--list-resources zeigt die niceId)")
        rid = match["resourceId"]
        detail = (api_get(endpoint, api_key, f"/resource/{rid}").get("data")) or {}
        targets = ((api_get(endpoint, api_key, f"/resource/{rid}/targets").get("data")) or {}).get("targets") or []
        extras: dict[str, list] = {}
        for name in ("roles", "users", "whitelist"):
            try:
                extras[name] = ((api_get(endpoint, api_key, f"/resource/{rid}/{name}").get("data")) or {}).get(name) or []
            except RuntimeError as exc:  # z. B. 403: dem Key fehlt die Berechtigung
                extras[name] = []
                print(f"[warnung] {name} nicht lesbar: {exc}", file=sys.stderr)
    except RuntimeError as exc:
        sys.exit(str(exc))

    has_hc = any(t.get("hcEnabled") for t in targets)
    if args.healthcheck:
        keep = args.healthcheck == "keep"
    else:
        keep = ask_yes_no("Health-Check uebernehmen (sonst enabled: false)", False) if has_hc else False
    resource, notes = build_import_resource(detail, targets, extras["roles"], extras["users"], extras["whitelist"], keep, args.hostname)
    for key_name, text in (("passwordId", "Passwort"), ("pincodeId", "PIN"), ("headerAuthId", "Basic Auth")):
        if match.get(key_name) is not None:
            notes.append(f"{text} gesetzt, aber nicht lesbar: als ${{PANGOLIN_..}}-Platzhalter von Hand ergaenzen")
    host_guess = (resource.get("targets") or [{}])[0].get("hostname") or ""
    service = args.service or (slug(host_guess) if host_guess and not re.fullmatch(r"[\d.:]+", host_guess) else "web")
    text = yaml.safe_dump({"services": {service: {match["niceId"]: resource}}}, sort_keys=False, width=10_000, allow_unicode=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"[geschrieben] {args.out}", file=sys.stderr)
    else:
        print(text, end="")
    for note in notes:
        print(f"[hinweis] {match['niceId']}: {note}", file=sys.stderr)
    return 0


def write_custom(path: Path, results: list[Result]) -> None:
    """Labels in die custom.yml einarbeiten (Kommentare bleiben dank ruamel.yaml erhalten).

    Bereits vorhandene Labels derselben Resource (gleicher Key) werden ersetzt,
    alle anderen Labels bleiben unveraendert. Vor dem Schreiben entsteht ein Backup.
    """
    try:
        from ruamel.yaml import YAML
    except ImportError:
        sys.exit("--write-custom braucht ruamel.yaml (erhaelt Kommentare): pip install ruamel.yaml")
    rt = YAML()
    rt.preserve_quotes = True
    rt.indent(mapping=2, sequence=4, offset=2)
    rt.width = 10_000

    existing = path.is_file()
    data = None
    if existing:
        original = path.read_text(encoding="utf-8")
        data = rt.load(original)
        path.with_name(path.name + ".bak").write_text(original, encoding="utf-8")
    if data is None:
        data = {}
    services = data.get("services")
    if services is None:
        services = {}
        data["services"] = services

    for res in results:
        svc = services.get(res.service)
        if svc is None:
            svc = {}
            services[res.service] = svc
        prefix = f"{LABEL_PREFIX}.{res.key}."
        labels = svc.get("labels")
        if labels is None:
            svc["labels"] = list(res.labels)
        elif isinstance(labels, dict):
            for old in [k for k in labels if str(k).startswith(prefix)]:
                del labels[old]
            for line in res.labels:
                k, v = line.split("=", 1)
                labels[k] = v
        else:
            for idx in reversed([i for i, e in enumerate(labels) if str(e).startswith(prefix)]):
                del labels[idx]
            labels.extend(res.labels)

    with path.open("w", encoding="utf-8") as fh:
        rt.dump(data, fh)


def main() -> None:
    parser = argparse.ArgumentParser(description="Pangolin-Blueprint-Labels aus Compose-Dateien generieren")
    parser.add_argument("compose", nargs="*", help="eine oder mehrere docker-compose-Dateien")
    parser.add_argument("-p", "--project", help="Projektverzeichnis: COMPOSE_FILE aus der .env lesen, alle Dateien zusammenfuehren, Labels der custom.yml zuordnen")
    parser.add_argument("--write-custom", action="store_true", help="(mit --project) Labels direkt in die custom.yml schreiben, Kommentare bleiben erhalten, Backup als custom.yml.bak (braucht ruamel.yaml)")
    parser.add_argument("-c", "--config", help="Konfigurationsdatei (Standard: ./pangolin-labels.yaml oder ~/.config/pangolin-labels/config.yaml)")
    parser.add_argument("-o", "--out-dir", help="je Compose-Datei eine <name>.pangolin.yml (Compose-Override) in dieses Verzeichnis schreiben")
    parser.add_argument("--org", help="feste Org: nur deren Domains anbieten (Standard: PANGOLIN_ORG aus der .env des Projekts)")
    parser.add_argument("--spec", help="nicht-interaktiv: Resources aus dieser YAML-Datei (services: <service>: <key>: <Blueprint-Resource>) validieren und nach --out schreiben")
    parser.add_argument("--out", help="(mit --spec) Zieldatei, ein Compose-Override")
    parser.add_argument("--check", action="store_true", help="(mit --spec) nichts schreiben, Exit-Code 1 wenn --out vom Soll abweicht")
    parser.add_argument("--list-resources", action="store_true", help="bestehende Resources der Org (--org) aus der API auflisten")
    parser.add_argument("--import", dest="import_id", metavar="NICEID", help="eine bestehende Resource als Spec-Eintrag ausgeben (niceId aus --list-resources)")
    parser.add_argument("--service", help="(mit --import) Compose-Service, der die Labels bekommt (Standard: Hostname des Targets)")
    parser.add_argument("--hostname", help="(mit --import) Hostname der Targets ersetzen, z. B. ${COMPOSE_PROJECT_NAME}-web")
    parser.add_argument("--healthcheck", choices=["keep", "disable"], help="(mit --import) Health-Check uebernehmen oder mit enabled: false uebernehmen (Standard: fragen)")
    parser.add_argument("--no-api", action="store_true", help="Pangolin-API nicht abfragen, nur Domains aus der Konfiguration verwenden")
    parser.add_argument("--list-domains", action="store_true", help="nur die verfuegbaren Domains anzeigen und beenden")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.list_resources or args.import_id:
        sys.exit(run_resources(args, cfg))
    org = args.org
    if args.project:
        project_dir = Path(args.project)
        site_cfg = read_site_config(project_dir)
        net = (site_cfg.get("system") or {}).get("newt_net")
        # Reihenfolge: --org, PANGOLIN_ORG (.env), Org des Newt-Netzes (net_orgs in der Config)
        org = org or read_env_file(project_dir / ".env").get("PANGOLIN_ORG") or (cfg.get("net_orgs") or {}).get(net)
        cfg["_site_domain"] = site_cfg.get("domain", "")
        cfg["_key_prefix"] = project_dir.resolve().name
    if org:
        cfg["orgs"] = [org]  # API nur fuer diese Org abfragen
    domains = get_domains(cfg, use_api=not args.no_api)
    if org:
        # Domains aus der Config ohne Org-Angabe bleiben erhalten (Org unbekannt)
        domains = [d for d in domains if d.org in ("", org)]
        print(f"[info] Org {org}: {len(domains)} Domain(s).")
        if not domains:
            sys.exit(f"Keine Domains fuer Org {org} gefunden (Schreibweise der Org-ID pruefen).")

    if args.list_domains:
        if not domains:
            print("Keine Domains gefunden (weder API noch Konfiguration).")
        for dom in domains:
            print(f"- {dom.label()}  [{dom.source}]")
        return
    if args.spec:
        if not args.out:
            parser.error("--spec braucht --out")
        sys.exit(run_spec(Path(args.spec), Path(args.out), args.check, domains))
    if not args.compose and not args.project:
        parser.error("Compose-Datei(en) oder --project angeben (oder --list-domains)")
    if args.write_custom and not args.project:
        parser.error("--write-custom funktioniert nur zusammen mit --project")
    if not domains:
        print("[hinweis] Keine Domains bekannt - Domainnamen muessen von Hand eingegeben werden.", file=sys.stderr)

    used_keys: set[str] = set()
    used_domains: set[str] = set()
    secrets_note: list[str] = []
    results: list[Result] = []

    # Gruppen: (Anzeigename, Ziel-Datei fuer die Labels, Services)
    groups: list[tuple[str, Path, list[ServiceInfo]]] = []
    custom_path: Path | None = None
    if args.project:
        project = Path(args.project)
        if not project.is_dir():
            sys.exit(f"Projektverzeichnis nicht gefunden: {project}")
        services, custom_path, files = load_project(project)
        names = ", ".join(f.name for f in files)
        groups.append((f"Projekt {project} ({names}) -> Labels fuer {custom_path.name}", custom_path, services))
        if custom_path not in files:
            print(f"[hinweis] {custom_path.name} steht nicht in COMPOSE_FILE - bitte dort ergaenzen.", file=sys.stderr)
    for file_arg in args.compose:
        path = Path(file_arg)
        if not path.is_file():
            sys.exit(f"Datei nicht gefunden: {path}")
        groups.append((str(path), path, load_services(path)))

    for title, target_file, services in groups:
        candidates = [s for s in services if not any(h in s.image for h in SKIP_IMAGE_HINTS)]
        print(f"\n##### {title} - {len(candidates)} Service(s)")
        for svc in candidates:
            outcome = configure_service(svc, cfg, domains, used_keys, used_domains, secrets_note)
            if outcome:
                key, labels = outcome
                results.append(Result(target_file, svc.name, key, labels))

    if not results:
        print("\nNichts zu veroeffentlichen - keine Labels erzeugt.")
        return

    print("\n" + "=" * 70)
    print("ERGEBNIS - diese Labels setzen:")
    print("=" * 70)
    for res in results:
        print("\n" + render_snippet(res))

    if args.write_custom and custom_path is not None:
        project_results = [r for r in results if r.compose_file == custom_path]
        if project_results:
            write_custom(custom_path, project_results)
            backup_note = f" (Backup: {custom_path.name}.bak)" if custom_path.with_name(custom_path.name + ".bak").is_file() else ""
            print(f"[geschrieben] {custom_path}{backup_note}")

    if args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        by_file: dict[Path, list[Result]] = {}
        for res in results:
            by_file.setdefault(res.compose_file, []).append(res)
        for compose_file, items in by_file.items():
            target = out_dir / f"{compose_file.stem}.pangolin.yml"
            target.write_text(render_override(items), encoding="utf-8")
            print(f"[geschrieben] {target}  ->  docker compose -f {compose_file} -f {target} up -d")

    print("Hinweise:")
    print("- Der Newt-Connector braucht Zugriff auf den Docker-Socket (DOCKER_SOCKET=/var/run/docker.sock)")
    print("  und muss die Container im selben Docker-Netzwerk erreichen.")
    print("- Labels werden fortlaufend angewendet: Aenderungen im Dashboard koennen ueberschrieben werden.")
    if secrets_note:
        names = ", ".join(sorted(set(secrets_note)))
        print(f"- Geheimnisse sind als Umgebungsvariablen-Platzhalter ausgegeben, bitte in .env setzen: {names}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAbgebrochen.")
