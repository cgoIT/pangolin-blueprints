# pangolin-labels

Gefuehrter Generator fuer Pangolin-Blueprint-Labels (`pangolin.public-resources.*`) aus einer oder mehreren Docker-Compose-Dateien.

> **Experimentell (v0.1):** bisher nur mit Beispielprojekten und einzelnen Abfragen gegen eine echte Instanz getestet, es gibt keine automatisierten Tests.
> Generierte Labels vor dem Einsatz pruefen, `--write-custom` nur in einem Git-Repo mit Diff verwenden.

## Installation

Python 3.9+. Empfohlen mit [pipx](https://pipx.pypa.io) (stellt den Befehl `pangolin-labels` bereit, ohne eigenes venv):

```bash
pipx install "pangolin-labels[write] @ git+https://github.com/cgoIT/pangolin-blueprints.git"
pipx upgrade pangolin-labels      # Update
```

`[write]` installiert zusaetzlich `ruamel.yaml` fuer `--write-custom`; ohne reicht `pangolin-labels @ git+...`.
Aus einem Checkout geht auch `pip install .` (oder `pip install -r requirements.txt` und direkt `python3 pangolin_labels.py ...`).

Die Konfiguration wird gesucht in `./pangolin-labels.yaml`, `~/.config/pangolin-labels/config.yaml`, `/etc/pangolin-labels/config.yaml`.

## Nutzung

```bash
cp config.example.yaml pangolin-labels.yaml   # anpassen
export PANGOLIN_API_KEY='<id>.<secret>'       # nur fuer die Domain-Abfrage per API

pangolin-labels --list-domains                 # Domains aus der API anzeigen
pangolin-labels stack-a/compose.yaml stack-b/compose.yaml
pangolin-labels compose.yaml -o out/           # zusaetzlich Compose-Override schreiben
pangolin-labels compose.yaml --no-api          # nur Domains aus der Konfiguration

# Projekte mit mehreren Dateien via COMPOSE_FILE (.env)
pangolin-labels -p /pfad/zum/projekt                  # Labels fuer custom.yml ausgeben
pangolin-labels -p /pfad/zum/projekt --write-custom   # direkt in custom.yml schreiben
```

### Projektmodus (`-p`)

- Liest `COMPOSE_FILE` aus der `.env` des Projekts. Trenner ist `COMPOSE_PATH_SEPARATOR`, sonst Komma (falls vorhanden) oder Doppelpunkt.
- Fuehrt alle Dateien zu einer Service-Sicht zusammen (Ports und Image aus allen Dateien, vorhandene `pangolin.*`-Labels werden erkannt).
- Ziel fuer die neuen Labels ist immer die `custom.yml`.
- Mit `--write-custom` werden die Labels dort eingetragen. Dafuer `pip install ruamel.yaml` (erhaelt Kommentare und Formatierung).
  Labels derselben Resource (gleicher Key) werden ersetzt, alle anderen Labels bleiben. Vorher entsteht `custom.yml.bak`.
- Nicht ausgewertet werden `include:` und `extends:` in den Compose-Dateien.

Pro Service wird abgefragt: veroeffentlichen?, Typ (http/tcp/udp), Resource-Key, Anzeigename, Domain + Subdomain,
Ziel-Port, Backend-Protokoll, optional Site, Authentifizierung (SSO, PIN, Passwort, Basic Auth) und erweiterte
Optionen (Identitaets-Header, Healthcheck, Request-Header, Resource-Labels).

Ausgabe: ein `labels:`-Block pro Service zum Einfuegen. Mit `-o DIR` zusaetzlich eine Override-Datei je
Compose-Datei, die du mit `docker compose -f compose.yaml -f DIR/compose.pangolin.yml up -d` zusammenfuehrst.
Die Original-Compose-Dateien werden nie veraendert (Kommentare und Formatierung bleiben erhalten).

## Verhalten und Entscheidungen

- **Mehrere Orgs:** `orgs: [a, b]` (oder `org_id`) in der Config. Ohne Angabe nutzt das Tool alle Orgs des API-Keys
  (`GET /v1/orgs`). Die Domainliste zeigt die Org je Domain an. Sites mit `org:` werden nur fuer Domains derselben Org vorgeschlagen.
  Feste Org pro Projekt: `--org meine-org` oder `PANGOLIN_ORG=meine-org` in der `.env` des Projekts (`-p`). Dann wird nur diese Org abgefragt und angeboten.

- **Domains gehoeren zur Organisation, nicht zur Site.** Der Site-Vorschlag kommt deshalb aus dem `sites:`-Block
  der Konfiguration (Site -> zugeordnete Basisdomains), nicht aus Pangolin.
- **Site-Label ist optional.** `targets[0].site` wird nur ausgegeben, wenn du eine Site waehlst. Sonst gilt die Site des
  Newt-Connectors, der die Labels liest.
- **Ports werden immer explizit gesetzt**, weil die Auto-Erkennung von Pangolin nur auf `expose` basiert.
- **Geheimnisse** (PIN, Passwort, Basic Auth) werden standardmaessig als `${PANGOLIN_<KEY>_...}`-Platzhalter ausgegeben,
  damit sie nicht im Klartext in der Compose-Datei landen. Variablen in `.env` setzen.
- Services mit Image `fosrl/*` (Newt) werden uebersprungen, Services mit vorhandenen `pangolin.*`-Labels nur nach Rueckfrage.
- Es wird nur das neue Schema (`public-resources`, `mode`, `request-headers`) ausgegeben.

## Noch zu pruefen / bewusst offen (v1)

- API-Pfad: es wird `<endpoint>/v1/org/<orgId>/domains` aufgerufen. Passt der Pfad bei deiner Installation nicht, in
  `api_url()` anpassen. Bei Fehlern faellt das Tool auf die Domains aus der Konfiguration zurueck.
- Domains vom Typ `cname` bekommen keine Subdomain-Abfrage (Annahme: es gilt nur der exakte Hostname).
- Healthcheck-Hostname wird auf den Service-Namen gesetzt. Passt das nicht zum Container-Namen, von Hand korrigieren.
- Mehrere Container, die zu einer Resource beitragen (zusaetzliche Targets, Load-Balancing), werden noch nicht unterstuetzt.
- Nicht abgedeckt: Rules, mTLS und Maintenance-Seite (EE), private Resources, Policies.