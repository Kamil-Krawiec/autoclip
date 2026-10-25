# Testowanie AutoClip

Instalacja i narzędzia: [CONTRIBUTING.md](CONTRIBUTING.md). Używaj Python
3.11/3.12, FFmpeg z libass, Node 20+; wersję potwierdzaj, nie zakładaj z PATH.

## Szybki baseline zgodny z CI

```bash
ruff check .
ruff format --check .
pytest -m "not slow and not golden and not e2e" -q
pytest -m "slow and not golden and not e2e" -q
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run build
docker compose -f docker/compose.yaml config --quiet
```

`golden` i `e2e` potrzebują właściwych materiałów; nie włączaj ich przypadkiem.
`tests/conftest.py` izoluje AUTOCLIP_HOME i keyring. LLM w baseline to mocki;
nie wykonuj płatnych wywołań ani pobierania modeli do testu UI.

## Weryfikacja w istniejącym obrazie, bez dotykania danych użytkownika

Repo montuj read-only, bez wolumenu produkcyjnego. Instalacja narzędzi jest
efemeryczna; zamień nazwę obrazu, jeśli lokalnie jest inna.

```bash
docker run --rm --entrypoint sh \
  -v "$PWD:/workspace:ro" -w /workspace \
  -e PYTHONPATH=/workspace/backend autoclip:latest -c '
  pip install --quiet pytest pytest-asyncio ruff &&
  ruff check --no-cache backend tests &&
  pytest -p no:cacheprovider -m "not golden and not e2e" -q'
```

To sprawdza kod z checkoutu na zależnościach istniejącego obrazu, **nie nowy
Dockerfile**. Nowy obraz wymaga osobnego build, importów ML i healthchecka.
Przy mount read-only używaj `ruff --no-cache` i `pytest -p no:cacheprovider`.

## Macierz edytora przed wydaniem

| Kontrakt | Dowód |
|---|---|
| Migracja i trwałość | DB v1 → nowa wersja, reopen po restarcie; stare słowa/styl/ratio zachowane |
| Ponowne użycie źródła | Nowy job ID, ten sam source ID, brak uploadu i utraty poprzednich edycji |
| Napisy | ON/OFF, polskie znaki, korekta tekstu, pozycja i rozmiar; realne klatki ASS |
| Framing | Rogi i tekst w syntetycznym 16:9; Auto/Fit/Manual, ratio, trim, zoom i seek |
| Izolacja | Edycja klipu A nie zmienia B; powolna odpowiedź A nie nadpisuje B |
| Eksport | ffprobe: wymiary/czas/audio; pełny decode; powtórne rendery zachowują stare pliki |
| Upload | loaded/total i cancel mock XHR; prawdziwe 2–5 GB: RSS, dysk, zerwanie i cleanup |
| Biblioteka | traversal, symlink poza root, brak pliku, uprawnienia, read-only mount |

Małe fixture'y wideo generuj FFmpeg w katalogu tymczasowym; nie commituj dużych
mediów. Testy filtrów tekstowych nie zastępują realnego renderu. Browser QA
obejmuje obsługę klawiatury i powrót do projektu po odświeżeniu.

Raportuj oddzielnie: unit/API, realne rendery, build, działającą przeglądarkę,
nowy Docker build, duży upload i realne AI. Brak wykonania to „nie sprawdzono”.

## Browser self-check M1

`tests/browser_source_reuse.cjs` używa opcjonalnie zainstalowanego Playwright
i Firefox, bez dodawania zależności do aplikacji. Uruchamiaj go wyłącznie na
świeżym kontenerze testowym z wyłączonym workerem; test tworzy queued job.
Poniższe komendy wykonuj z katalogu repo po `npm --prefix frontend run build`.
Nie podpinaj istniejącego wolumenu ani konfiguracji API.

```bash
docker run --rm -d --name autoclip-m1-qa \
  -p 127.0.0.1:19879:8000 \
  -e AUTOCLIP_HOME=/tmp/autoclip-m1-qa -e AUTOCLIP_NO_WORKER=1 \
  -e PYTHONPATH=/workspace/backend -v "$PWD:/workspace:ro" -w /workspace \
  --entrypoint python autoclip:latest \
  -m uvicorn autoclip.app:create_app --factory --host 0.0.0.0 --port 8000
```

Po odpowiedzi `/api/health` zasiej fixture w efemerycznym kontenerze:

```bash
docker exec -i autoclip-m1-qa python - <<'PY'
from autoclip import db
from autoclip.db import store
from autoclip.db.models import Source, Job, Clip, ClipEdit
db.init()
store.create_source(Source(id="m1-source", type="upload", path="/tmp/fixture.mp4",
    title="Kod Zero test source", duration_s=180, width=1920, height=1080))
store.create_job(Job(id="m1-old-job", source_id="m1-source", status="done",
    provider="ollama", progress=1))
store.replace_clips("m1-old-job", [Clip(id="m1-old-clip", job_id="m1-old-job",
    start_s=10, end_s=35, title="Previous edited short")])
store.upsert_clip_edit(ClipEdit(clip_id="m1-old-clip", caption_style="clean_lower",
    ratio="1:1", edited_words=[{"text":"Poprawione", "start":10, "end":11,
    "speaker":None}]))
PY
```

Uruchom skrypt; jeżeli Playwright nie jest dostępny przez standardowe resolution,
ustaw `AUTOCLIP_PLAYWRIGHT_MODULE` na jego lokalny moduł. Dla zewnętrznego cache
przeglądarek ustaw też `PLAYWRIGHT_BROWSERS_PATH`. Nie instaluj ich w runtime app.

```bash
node tests/browser_source_reuse.cjs
docker stop autoclip-m1-qa
```

Sukces: JSON `passed:true`, `jobPosts:1`, `ingestRequests:0`; screenshot domyślnie
w `/tmp/autoclip-m1-browser.png`. Nowy kontener na każde uruchomienie zapewnia
identyczną historię. Skrypt sprawdza integrację UI/API, nie ML ani dekodowanie
mediów. Przykłady fixture są syntetyczne i nie wymagają prawdziwego filmu.
