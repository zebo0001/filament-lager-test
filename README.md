# Filament-Lager (Testversion)

Eigenständiges Lagerverwaltungs-System für 3D-Druck-Filament: [Spoolman](https://github.com/Donkie/Spoolman)
für die Spulenverwaltung, plus ein eigenes Lagerplatz-Modul, das Spulen konkreten physischen Plätzen
(Drucker, ACE-Einheit, Drybox, ...) zuordnet — inklusive Drag & Drop, Einkaufsliste bei leeren Rollen und
visueller Lager-Ansicht.

Diese Version ist bewusst eigenständig gehalten (reines `docker compose`) und läuft ohne die
Produktiv-Infrastruktur (Docker Swarm, Traefik, HAProxy, GlusterFS), auf der das Original läuft —
das heißt: läuft unverändert auf **Docker Desktop** (Windows/Mac) oder jedem Linux-Host mit
Docker + Compose-Plugin.

## Screenshot

![Lagerplätze-Ansicht](docs/dashboard-screenshot.png)

## Voraussetzungen

- Docker Desktop (Windows/Mac) oder Docker + Compose-Plugin (Linux)

## Starten

```bash
docker compose up -d
```

Danach:

- Dashboard: http://localhost:8093
- Spoolman (vollständige Oberfläche): http://localhost:8091

Ports lassen sich über eine `.env`-Datei anpassen (Vorlage: `.env.example`).

## Was ist zu testen?

- Spulen in Spoolman anlegen (Material, Hersteller, Farbe)
- Lagerplätze konfigurieren (Dashboard → Lagerplätze): Drucker, ACE-Einheit, Drybox
- Spulen per Drag & Drop bzw. Zuweisungsdialog auf Plätze legen
- "Rolle leer" testen (archiviert die Spule in Spoolman, setzt einen Eintrag auf die Einkaufsliste)
- Mehrfarbiges Filament anlegen und in der Lager-Ansicht prüfen

Rückmeldungen, Bugs, Verbesserungsvorschläge gerne als Issue in diesem Repo.

## Aufbau

```
docker-compose.yml
dashboard/     Frontend (nginx, statische SPA) + Reverse-Proxy zu Spoolman/Storage
storage/       Eigenes Lagerplatz-Backend (FastAPI + SQLite)
```

Spoolman selbst läuft als offizielles Image (`ghcr.io/donkie/spoolman`), Konfiguration/Doku dort:
https://github.com/Donkie/Spoolman

## Daten

Beide Datenbanken (Spoolman, Lagerplätze) starten leer und liegen in benannten Docker-Volumes
(`spoolman_data`, `storage_data`). Zum Zurücksetzen: `docker compose down -v`.

## Status

Frühe Testversion — nicht für den produktiven Einsatz gedacht, kein Support-Anspruch.
