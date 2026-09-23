# Filament-Lager

Eigenstaendiges Lagerverwaltungs-System fuer 3D-Druck-Filament: [Spoolman](https://github.com/Donkie/Spoolman) fuer die Spulenverwaltung, plus ein eigenes Lagerplatz-Modul, das Spulen konkreten physischen Plaetzen (Drucker, ACE-Einheit, Drybox, ...) zuordnet - inklusive Drag & Drop, Einkaufsliste bei leeren Rollen, visueller Lager-Ansicht, einem eigenen Label-Designer fuer Etiketten (mit QR/Barcode-Export als PNG/PDF) und einem scan-gestuetzten Workflow fuer Wareneingang und Lagerplatz-Zuordnung (Hardware-Scanner oder Handykamera).

Laeuft bei uns produktiv seit mehreren Wochen. Ruerckmeldungen, Bugs und Verbesserungsvorschlaege sind sehr willkommen - gerne als Issue in diesem Repo.

## Screenshot

![Lagerplaetze-Ansicht](docs/dashboard-screenshot.png)

## Voraussetzungen

- Docker Desktop (Windows/Mac) oder Docker + Compose-Plugin (Linux)
- Der `storage`-Dienst baut auf einem Playwright/Chromium-Basisimage auf (fuer den Label-PNG/PDF-Export) - das Image ist entsprechend groesser, der erste `docker compose up -d` dauert daher beim Bauen etwas laenger als bei einem schlanken Python-Image.

## Starten

```bash
docker compose up -d
```

Danach:

- Dashboard: http://localhost:8093
- Spoolman (vollstaendige Oberflaeche): http://localhost:8091

Ports lassen sich ueber eine `.env`-Datei anpassen (Vorlage: `.env.example`).

**Wichtig fuer den Kamera-Scan:** Kamera-basiertes QR-Scannen (im Scan-Modus, als Alternative zu einem Hardware-Scanner) braucht laut Browser-Vorgabe einen sicheren Kontext (HTTPS oder `localhost`). Ueber `http://localhost:8093` auf demselben Rechner funktioniert das direkt. Greift man vom Handy aus ueber die LAN-IP des Docker-Hosts zu (z.B. `http://192.168.x.x:8093`), ist die Kamera-Option automatisch deaktiviert (mit Hinweistext) - dann bleibt nur ein externer Hardware-Barcode-/QR-Scanner (HID-Tastatur-Emulation) als Eingabeweg, oder man stellt den Dashboard-Zugriff selbst per Reverse-Proxy auf HTTPS um.

## Was ist zu testen?

Grundfunktionen:

- Spulen in Spoolman anlegen (Material, Hersteller, Farbe, auch mehrfarbig), inkl. Artikelnummer-Feld
- Lagerplaetze konfigurieren (Dashboard -> Lagerplaetze): Drucker, ACE-Einheit, Drybox
- Spulen per Drag & Drop bzw. Zuweisungsdialog ('Auf Drucker/ACE', 'In Drybox') auf Plaetze legen
- Auf einen Spulennamen in der Tabelle klicken -> Detail-/Edit-Modal oeffnet sich (Preis, Gewichte, Lot-Nr., Kommentar, Temperatur-Overrides, Filament-Typ-Felder) - auch nachtraegliche Bearbeitung moeglich
- 'Rolle leer' testen (archiviert die Spule in Spoolman, gibt den Platz frei, setzt einen Eintrag auf die Einkaufsliste inkl. Artikelnummer)
- Suche und Pagination im Spulenverzeichnis
- Panel-Reihenfolge im Dashboard ueber den Einstellungen-Button anpassen

Label-Designer (eigener Menuepunkt im Dashboard):

- Eigene Etikettenvorlagen per Drag & Drop zusammenstellen (Text mit Spoolman-Variablen wie Name/Material/Hersteller/Farbe, Spulen-Icon, Farbfeld, Barcode/QR-Code)
- Vorlagen sind nach Spule oder Filament typisiert (Umschalter im Editor) - der QR-Code-Inhalt folgt dabei automatisch dem gewaehlten Typ
- Live-QR-Vorschau direkt im Design-Tab (kein Platzhalter mehr, echter QR-Code waehrend der Bearbeitung)
- Design- und Druck-Tab getrennt: im Druck-Tab lassen sich mehrere Spulen/Filamente fuer einen Etikettenbogen (Sheet-Tile-Druck) auswaehlen, inkl. Live-Vorschau des Bogens
- QR-Code enthaelt automatisch das App-Logo im Zentrum eingebettet
- QR-Inhalt folgt dem Schema `WEB+SPOOLMAN:S-<spool_id>` bzw. `WEB+SPOOLMAN:F-<filament_id>` (kompatibel zum Scan-Format des Spoolman-eigenen Label-Designers, Gross-/Kleinschreibung spielt beim Scannen keine Rolle)
- Vorlagen als PNG oder PDF exportieren (laeuft ueber Headless Chromium im `storage`-Dienst) und mit einem Etikettendrucker/Laserdrucker ausdrucken

Scan-gestuetzter Workflow (Hardware-Barcode-/QR-Scanner mit HID-Tastatur-Emulation, kein Treiber noetig - oder wahlweise die Handykamera, siehe HTTPS-Hinweis oben):

- Spulen-ID wird direkt in der Spulen-Tabelle angezeigt
- Slot-Label-Generierung und Druckansicht fuer Lagerplaetze
- Scan-Overlay mit Statusanzeige beim Scannen, Kamera-Scan optional per Toggle aktivierbar (Einstellung wird pro Geraet im Browser gespeichert)
- Vollstaendiger Scan-only Wareneingang: ein gescannter Filament-QR-Code legt automatisch eine neue Spule an, ein anschliessend gescannter Platz-Code weist sie direkt zu - ganz ohne manuelle Dialog-Interaktion
- Im 'Neue Spule'-Dialog kann alternativ ein gedrucktes Filament-QR-Label gescannt werden, um Material/Hersteller/Farbe automatisch vorzubefuellen

## Aufbau

```
docker-compose.yml
dashboard/   Frontend (nginx, statische SPA) + Reverse-Proxy zu Spoolman/Storage
storage/     Eigenes Lagerplatz- und Label-Backend (FastAPI + SQLite + Playwright/Chromium fuer Label-Export)
```

Spoolman selbst laeuft als offizielles Image (`ghcr.io/donkie/spoolman`), Konfiguration/Doku dort: https://github.com/Donkie/Spoolman

## Daten

Beide Datenbanken (Spoolman, Lagerplaetze/Label-Vorlagen) starten leer und liegen in benannten Docker-Volumes (`spoolman_data`, `storage_data`). Zum Zuruecksetzen: `docker compose down -v`.

## Bekannte Einschraenkungen (Stand dieser Version)

- Kamera-Scan ist nur in einem sicheren Kontext (HTTPS oder `localhost`) verfuegbar - siehe Hinweis oben.

## Status

In aktiver Entwicklung. Rueckmeldungen, Bug-Reports und Feature-Wuensche sind herzlich willkommen - am liebsten als Issue in diesem Repo.
