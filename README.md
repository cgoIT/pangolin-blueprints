# pangolin-labels

Gefuehrter Generator fuer Pangolin-Blueprint-Labels (`pangolin.public-resources.*`) aus einer oder mehreren Docker-Compose-Dateien.

> **Experimentell (v0.2):** bisher nur mit Beispielprojekten und einzelnen Abfragen gegen eine echte Instanz getestet, es gibt keine automatisierten Tests.
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
## Nicht-interaktiv (`--spec`)

Fuer wiederholbare Laeufe (Skripte, Frameworks, CI) beschreibt eine YAML-Datei die Resources statt der Abfrage.
Struktur: `services:` > Name des Compose-Services > Resource-Key > Resource im Blueprint-Format
([Referenz](https://docs.pangolin.net/manage/blueprints)). `spec.yml`:

```yaml
services:
  web:                              # Compose-Service, der die Labels bekommt
    meine-seite-web:                # Resource-Key (stabile ID, pro Org eindeutig)
      name: Meine Seite
      mode: http
      full-domain: example.com
      targets:
        - hostname: "${COMPOSE_PROJECT_NAME}-web"   # Compose ersetzt Variablen in Labels
          method: http
          port: 8080
  admin:
    meine-seite-admin:
      name: Meine Seite - Admin
      mode: http
      full-domain: admin.example.com
      auth: { sso-enabled: true, sso-roles: [Member] }
      forward-identity-headers: false
      targets:
        - hostname: "${COMPOSE_PROJECT_NAME}-admin"
          method: http
          port: 80
          healthcheck: { hostname: "${COMPOSE_PROJECT_NAME}-admin", port: 80, path: / }   # optional
  mqtt:
    meine-seite-mqtt:
      name: MQTT
      mode: tcp
      proxy-port: 1883
      targets:
        - { hostname: mqtt, port: 1883 }
```

Aufruf:

```bash
pangolin-labels --spec spec.yml --out compose.pangolin.yml            # schreibt die Datei
pangolin-labels --spec spec.yml --out compose.pangolin.yml --check    # schreibt nichts, Exit 1 bei Abweichung
```

- `--out` ist ein Compose-Override (`services: <name>: labels: [...]`) mit Kommentar `AUTO-GENERATED`. Er gehoert in `COMPOSE_FILE`
  (z. B. `compose.yml:compose.pangolin.yml`); danach die Container neu erstellen (`docker compose up -d`).
- Mit `--org meine-org` oder `-p projekt` (Org aus `PANGOLIN_ORG` bzw. `net_orgs`) wird die Domain gegen die Domains dieser Org geprueft.
  Eine Domain ausserhalb der Org ergibt eine Warnung. `--no-api` ueberspringt die API-Abfrage.
- Validiert werden: `targets` mit `hostname`/`port`, bei `http` `full-domain` und `method`, bei `tcp`/`udp` `proxy-port`
  (ohne `method` und `auth`), keine doppelten Keys oder `full-domain` in einer Spec. Bei Fehlern Exit 2 und es wird nichts geschrieben.
- Exit-Codes: `0` ok, `1` Abweichung (nur mit `--check`), `2` Spec ungueltig.
- Wiederholte Laeufe aendern die Datei nicht (nur bei Unterschieden wird geschrieben), `--check` eignet sich fuer CI und Health-Checks.

## Bestehende Resources uebernehmen (`--list-resources`, `--import`)

Resources, die du im Dashboard angelegt hast, lassen sich als Spec-Eintrag aus der API lesen (nur lesend, der API-Key braucht
Leserechte auf Resources, Rollen und Benutzer):

```bash
pangolin-labels --list-resources --org meine-org          # niceId, Domain, Site, an/aus, Health-Check
pangolin-labels --import <niceId> --org meine-org --service web --out spec.yml
```

- Die `niceId` ist der Resource-Key: Pangolin sucht beim Anwenden die Resource mit diesem Identifier in der Org und aktualisiert sie
  (laut Pangolin-Quellcode). Dabei werden auch Auth-Einstellungen mit den Werten der Spec ueberschrieben (z. B. `sso` auf `false`, wenn es fehlt).
  Alles Lesbare wird deshalb uebernommen; Passwort, PIN und Basic Auth muessen von Hand ergaenzt werden. Erst an einer unkritischen Resource testen.
- Den Identifier kann man in Pangolin aendern (Buchstaben, Zahlen, Bindestriche, pro Org eindeutig). Wer sprechende Keys will, benennt zuerst
  die Resource um und importiert sie danach (oder schreibt die Spec mit dem neuen Key).
- `--healthcheck keep|disable`: Health-Check uebernehmen oder mit `enabled: false` (Werte bleiben erhalten). Ohne Angabe wird gefragt.
- `--hostname '${COMPOSE_PROJECT_NAME}-web'` ersetzt den Hostnamen der Targets (auch im Health-Check). `--service` bestimmt den Compose-Service.
- Uebernommen werden Name, Domain/`proxy-port`, SSO (Rollen ohne Admin, Benutzer, Whitelist), Identity-Header, Host-Header, TLS-Servername,
  Request-/Response-Header, Targets (Port, Methode, Pfad-Regeln, an/aus) und Health-Check. Die Site der Targets wird weggelassen (es gilt die Site des Connectors).
- Nicht lesbar oder nicht uebernommen (wird auf stderr als `[hinweis]` gemeldet): Passwort, PIN, Basic Auth, Regeln, mTLS, Wartungsmodus,
  Sticky Session, Load-Balancing-Strategie.

## Uebersicht: was ist schon umgestellt (`--status`)

```bash
pangolin-labels --status --org meine-org sites/*/compose.pangolin.yml andere/compose.yml
pangolin-labels --status --org meine-org -p /pfad/zum/projekt
```

Gleicht die `pangolin.public-resources.<key>`-Labels der angegebenen Compose-Dateien mit den Resources der Org ab (nur lesend):

- `[umgestellt]` Label-Key = Identifier einer Resource.
- `[umbenennen]` Resource mit gleicher Domain, aber anderem Identifier: Identifier in Pangolin auf den Label-Key aendern (sonst entsteht ein Konflikt).
- `[nur Label]` Label ohne Resource in dieser Org: wird beim Anwenden neu angelegt, oder der Key bzw. die Org stimmt nicht.
- `[offen]` Resource ohne Label.

Labels anderer Hosts werden nur gesehen, wenn ihre Compose-Dateien angegeben werden (dort aufrufen oder Dateien sammeln).

## Entwicklung

```bash
pip install -e ".[write]" pytest ruff
ruff check . && pytest
```
