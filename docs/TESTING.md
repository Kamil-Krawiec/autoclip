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
