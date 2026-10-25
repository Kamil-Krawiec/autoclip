# AutoClip Editor Implementation Plan

> **For agentic workers:** Implementuj zadania kolejno przy użyciu
> `superpowers:executing-plans` lub `superpowers:subagent-driven-development`,
> jeżeli te skille są dostępne. Instrukcje użytkownika mają pierwszeństwo.

**Cel:** poprawić wygodę edycji istniejących Shortsów, wybór materiałów i reframe
16:9 → 9:16, zachowując AutoClip i działającą selekcję AI.

**Architektura:** rozszerzamy istniejący `ClipEdit`, API i renderer FFmpeg.
Jeden backendowy resolver wyznacza efektywne ustawienia/kadr zarówno dla
preview, jak i eksportu. Źródło, zadanie analizy i edycja klipu pozostają
oddzielne; ponowna analiza tworzy nowy job i zachowuje stare klipy.

**Stack:** FastAPI, Pydantic, SQLite, React/TypeScript/Vite, FFmpeg/libass.
**Spec:** [EDITOR-SPEC.md](EDITOR-SPEC.md); stan bazowy w `FEATURES.md`.

## Ograniczenia wspólne

- CPU first, Docker Compose, Python 3.11/3.12, brak obowiązkowego GPU.
- Bez zmiany fundamentu projektu, selekcji momentów i boundary refinement.
- Stare dane czytelne; migracje append-only i domyślne zachowanie Auto.
- Sekrety pozostają lokalne; biblioteka tylko z jawnie dozwolonego katalogu.
- Łączny płatny DeepSeek ≤ **0,40 USD**; obecnie nie wykonano płatnych wywołań.
- Worker nie edytuje modeli/lockfile należących do innego zadania.

## Routing i kolejność

Preferowany wykonawca to `agy` **gemini-4-argon-low**, potwierdzony katalogiem
CLI 2026-10-25. Flash dla ograniczonych zadań UI/testów, free OpenCode dla
ekstrakcji i mechanicznych zmian po potwierdzeniu ceny. Integrator sam weryfikuje
diff i wyniki. DeepSeek jest rezerwą, nie obowiązkowym etapem.

Nie znamy pozostałych limitów AGY/Codex ani czasu resetu. Nie deklarujemy
oszczędności ani gwarancji pojemności. Najpierw jeden worker; najwyżej dwa
niezależne zadania po sprawdzeniu dostępności. Zachowaj ok. 25% dostępnej
pojemności na integrację jako heurystykę, jeśli limit daje się zmierzyć.

| Etap / branch | Właściciel | Zależności | Trudność | Stan |
|---|---|---|---|---|
| M0 `docs/editor-orchestration-plan` | koordynator + audyt Argon | brak | mała | audyt i przygotowanie |
| M1 `feat/source-reuse` | Argon | M0 | mała | plan |
| M2 `feat/clip-edit-state` | Argon; wyłączny właściciel modeli | M1 | średnia | plan |
| M3 `feat/fit-preview` | Argon | M2 | średnia | plan |
| U1 `feat/upload-progress` | Flash lub Argon | M1; bez zmian modeli edycji | średnia | plan |
| M4 `feat/manual-framing` | Argon | M3 | duża | plan |
| M5 `feat/caption-studio` | Argon | M4 | duża | plan |
| M6 `feat/local-media-library` | Argon + osobny review bezpieczeństwa | U1 | średnia/duża | plan |

Budżet czasu orientacyjny: mała paczka 1–3 h, średnia 3–8 h, duża 1–3 dni
z weryfikacją. To nakład inżynierski, nie obietnica czasu odpowiedzi modelu.
Koszt API nie wynika z tych szacunków. Dla każdej paczki limit 1 zadanie + 1
korekta; po powtórnym niepowodzeniu checkpoint i zmiana podejścia.

## Review focus

- Stary projekt bez nowych pól: identyczne Auto i brak migracji niszczącej dane.
- Trim, ratio i opóźnione odpowiedzi: preview/MP4 oraz aktywny klip zachowują sens.
- Anulowany/zerwany upload i pełny dysk: czytelny błąd, brak osieroconych plików.
- Dwa eksporty tego samego klipu: izolowane pliki i brak niekontrolowanej konkurencji CPU.
- Ścieżki/symlinki spoza biblioteki: odmowa, bez ujawnienia plików serwera.

## M0 — przygotowanie

- [x] Odczyt kodu API, DB, pipeline'u i edytora; ustalenie reuse zamiast nowego silnika.
- [x] Fork `Kamil-Krawiec/autoclip`, origin/upstream, osobna gałąź, zachowane lokalne Docker diffy.
- [x] Instrukcje agentów, mapa funkcji, plan, procedura testów i handoff.
- [x] Baseline testów i częściowy wynik audytu Argona wpisany do handoffu.
- [ ] Osobny PR poprawek runtime po potwierdzeniu ich testami: lokalne libegl1,
  libgles2 i `av<19` nie są częścią upstream na sprawdzonym HEAD. Nie przenoś
  ich bezrefleksyjnie do zależności wszystkich platform. Zweryfikuj import
  MediaPipe oraz rzeczywiste dekodowanie PyAV/faster-whisper w nowym obrazie.

## M1 — wracanie do materiałów i projektów

**Pliki:** `frontend/src/pages/Ingest.tsx`, `frontend/src/api.ts`,
`backend/autoclip/api/jobs.py`, `tests/test_api.py`.
**Wejście:** istniejące `api.listSources()`, `api.createJob(sourceId, settings)`,
`GET /api/jobs`, `POST /api/jobs`. Nie tworzymy nowej tabeli projects.
**Wynik:** wybór „Existing source”, „Open clips” i „Process again”. Nowy job
odwołuje się do tego samego source; edycja istniejącego klipu nie wywołuje LLM.

- [ ] Dodaj test `test_reprocess_source_preserves_previous_job_and_edits`:
  dwa różne job ID, ten sam source ID, stare ClipEdit i exports nadal dostępne.
- [ ] Dodaj test brakującego source ID (404); jeżeli sprawdzasz obecność pliku,
  przygotuj prawdziwy mały plik w fixture, nie zmieniaj globalnie kontraktu testów.
- [ ] Udostępnij listę zapisanych źródeł i rozdziel otwarcie klipów od nowej analizy.
- [ ] Odśwież stronę i ponownie otwórz poprzedni job; sprawdź zachowanie ustawień.
- [ ] Uruchom `pytest tests/test_api.py tests/test_db.py -q` i frontend build.

**Akceptacja:** ponowna analiza bez transferu pliku, stary projekt nienaruszony.
**Ryzyko:** nowy job ponawia kosztowne etapy; współdzielenie cache między jobami
odkładamy do czasu zaprojektowania jawnej identyfikacji modelu i ustawień.

## M2 — trwały stan edycji i bezpieczny eksport

**Pliki:** `db/schema.py`, `db/models.py`, `db/store.py`, `api/schemas.py`,
`api/clips.py`, `pipeline/export.py`, `frontend/src/api.ts`,
`pages/Review.tsx`, `components/ClipPlayer.tsx`; testy DB/API/render.

**Kontrakt planowany:** rozszerz ClipEdit kolumnami `burn_captions` (true),
`framing_json` i `caption_overrides_json` (NULL = domyślne). Nowa migracja.
API zwraca pełny zapisany stan w ClipOut; dodaj `PATCH /api/clips/{id}/edit`
z częściową aktualizacją. Obecny endpoint `/captions` pozostaje zgodny i używa
tego samego zapisu. Framing: `{mode: 'auto'|'fit'|'manual', center_x, center_y,
zoom}`; współrzędne znormalizowane, finite, 0..1; zoom 1..4, tylko dla manual.
Pola manual są walidowane razem. Reset framing do auto nie usuwa napisów.
Automatyczny cache pozostaje nietknięty. Caption overrides są rzadkimi
nadpisaniami pól istniejącego CaptionStyle, nie kopią całego presetu.

- [ ] Najpierw test migracji v1 z zapisanymi edycjami i eksportami oraz test
  patchowania jednej sekcji bez utraty innych, w tym pustej listy słów.
- [ ] Wprowadź model i zapis; zachowaj semantykę None kontra pustych wartości.
- [ ] Rozwiąż zapisany stan w backendzie dla eksportu; nie polegaj na domyślnych
  parametrach klienta. Stare jawne ratio/style żądania zachowują kompatybilność.
- [ ] Dodaj napisy ON/OFF w Review i preview; przekaż istniejący burn_captions.
- [ ] Eksport pisze do unikalnego miejsca z export ID; publikuj plik po sukcesie,
  zachowaj poprzednie pliki. Wprowadź pojedynczy limit renderu dla review API.
- [ ] Chroń zmianę klipu przed utratą niezapisanych słów i spóźnioną odpowiedzią.
- [ ] Export zapisuje brudne słowa przed renderem albo blokuje render z jasnym
  komunikatem. `clip_words` respektuje aktualny trim również dla edited_words;
  filtruj efektywną listę bez kasowania zapisanych korekt poza chwilowym trimem.
  Test: popraw słowo → skróć → rozszerz → korekta zachowana, MP4 tylko w granicach.
- [ ] Testy: OFF usuwa filtr ASS, re-open zachowuje OFF, dwa eksporty mają
  różne ścieżki, błąd renderu nie niszczy poprzedniego eksportu.

**Akceptacja:** zapis → odświeżenie → eksport zachowuje edycje; stare projekty działają.
**Ryzyko:** wspólne modele — M3/M4/M5 czekają na integrację tego kontraktu.

## M3 — Fit + blurred background i zgodność kadru

**Pliki:** `api/clips.py`, nowy `pipeline/framing.py`,
`pipeline/reframe/croppath.py`, `frontend/src/components/ClipPlayer.tsx`,
`pages/Review.tsx`, `frontend/src/api.ts`, `tests/test_reframe.py`,
`tests/test_export_render.py`, `tests/test_api.py`.

**Kontrakt:** `resolve_crop_path(source, clip, job, edit, cached_path) -> CropPath`
w `pipeline/framing.py` jest jedynym resolverem Auto/Fit/Manual. Endpoint
crop-path zwraca efektywny plan dla zapisanego ratio/trim; eksport używa tego
samego resolvera. Cache ma sprawdzane ratio bazowe i granice. Przy trimie
przytnij/przesuń segmenty i keyframe'y, zablokuj rozszerzenie poza źródło;
dla obszarów bez cache użyj jawnego centre crop, nie starej osi czasu.

- [ ] Testy resolvera: stary auto, fit całej klatki, 1:1/16:9, trim i brak cache.
- [ ] Fit buduje segment z `fit=True` i korzysta z istniejącego `_fit_chain`.
- [ ] Dodaj Auto/Fit do Review. Preview: zsynchronizowane tło cover+blur i
  foreground contain; warstwa tła bez drugiego audio, zgodny seek/pause.
- [ ] FFmpeg fixture z oznaczonymi czterema rogami: Fit zachowuje wszystkie,
  wynik 1080×1920, poprawny czas i audio. Przetestuj film pionowy i kwadratowy.
- [ ] Porównaj browser i wyrenderowane klatki przy zmianie ratio, seek i trim.

**Akceptacja:** całe źródło widoczne, rozmyte tło, wybór zachowany po restarcie,
Auto bez regresji. Blur CSS nie gwarantuje identycznego kernela jak boxblur.
Dokładny proof zapewnia krótki render z produkcyjnego FFmpeg.

## U1 — rzeczywisty postęp uploadu

**Pliki:** `frontend/src/api.ts`, `pages/Ingest.tsx`, `api/sources.py`,
`tests/test_api.py`; wydziel komponent postępu tylko jeśli poprawi czytelność.
Nie współedytuj Ingest/api.ts z M1; U1 zaczyna się po integracji M1.

- [ ] Test kontraktu klienta: onprogress z loaded/total, abort, błąd sieci,
  brak computable total, odpowiedź błędu API; użyj mock XHR i małego self-check.
- [ ] `uploadSource(file, {onProgress, signal})` używa XMLHttpRequest;
  zachowaj istniejący ApiError i Promise<Source>. Abort nie uruchamia createJob.
- [ ] Pokaż bajty/%, wygładzoną prędkość i ETA tylko przy wiarygodnej próbce;
  100% transferu oznacza oczekiwanie na walidację, nie ukończoną analizę.
- [ ] Testuj suffix, fałszywe media, rozłączenie, ENOSPC i cleanup w finally.
  Sprawdź rzeczywisty lifecycle multipart/spool; zamknij UploadFile.
- [ ] Wprowadź jawny limit akceptujący co najmniej 5 GiB, odrzucanie 413,
  ochronę dysku i komunikat użytkownika; nie tylko kontrolę w JS.
- [ ] Ręczny test 2–5 GB: RSS, zużycie /tmp i /data, cancel i zerwane SSH.

**Akceptacja:** mierzalny transfer i cancel; brak mylenia upload/AI.
**Decyzja:** XHR w pierwszej wersji. Tus ocenić po rzeczywistym teście zerwania;
nie obiecujemy wznawiania i nie tworzymy własnego protokołu chunkowania.

## M4 — ręczny crop i zoom

**Pliki:** resolver M3, `pages/Review.tsx`, `components/ClipPlayer.tsx`,
nowy `components/FramingEditor.tsx`, testy resolvera/API/render.

- [ ] Testuj mapowanie center/zoom na parzyste okno CropPath przy krawędziach,
  zoom 1 i 4, różnych ratio, NaN/Infinity i niepoprawnych wartościach API.
- [ ] Manual: rozmiar bazowego crop z `target_crop_size` podziel przez zoom,
  dopasuj parzyste wymiary, clamp pozycji do źródła; statyczny keyframe.
- [ ] Pokaż źródło z ramką, drag, suwak zoom, kontrolki klawiaturowe, Reset Auto
  i porównanie. Safe-area jest wyłącznie overlayem edytora.
- [ ] Testy zapisu/reopen, niezależności klipów i pozycji w realnym MP4.

**Akceptacja:** crop i zoom wpływają na eksport, nie wyłącznie CSS.
Screen Focus to nazwany preset istniejącej geometrii, bez nowego silnika;
Fit zachowuje cały terminal, Manual pozwala powiększyć wybrany obszar.
Nie obiecuj odzyskania szczegółów przez upscale. AI super-resolution pomijamy:
koszt CPU i możliwe zniekształcenie tekstu nie służą temu workflow.

## M5 — Caption Studio

**Pliki:** `pipeline/captions.py`, model/API M2, `components/CaptionEditor.tsx`,
`components/ClipPlayer.tsx`, `pages/Review.tsx`, testy captions/API/render.

- [ ] Walidowane overrides istniejącego CaptionStyle: font z allowlisty Anton/
  Inter, size_ratio, primary/accent, outline, outline_width, shadow, max_words,
  boxed/box_colour/box_alpha i pozycja znormalizowana. Dokładne zakresy oprzyj
  na istniejących presetach i sprawdź w trzech ratio przed udostępnieniem UI.
- [ ] Backend nakłada overrides na kopię presetu; nie mutuje PRESETS.
- [ ] Dodaj pozycję x/y do ASS i preview oraz drag z równoważną obsługą klawiatury.
- [ ] Zachowaj 4 presety; dodaj One Word i Kod Zero dopiero po sprawdzeniu
  czytelności. Testuj polskie znaki, długie słowa, timing i reset sekcji.
- [ ] Endpoint krótkiego preview korzysta z tego samego resolvera i ASS co MP4,
  z limitem czasu i pojedynczym renderem CPU; nie przenosi sekretów do klienta.
- [ ] Zapis/reopen, tekst po ponownym eksporcie, rozmiar/pozycja w rzeczywistych
  klatkach; test wyłączenia napisów oraz niezależności dwóch klipów.

**Akceptacja:** poprawiony tekst i wygląd utrzymane, dokładny proof w renderze.
Animacja HTML jest orientacyjna; nie udawaj pikselowej zgodności z libass.

## M6 — lokalna biblioteka i dalszy workflow

**Pliki:** `api/sources.py`, `pipeline/ingest.py`, `api/schemas.py`,
`pages/Ingest.tsx`, `frontend/src/api.ts`, `docker/compose.yaml`, testy API.

- [ ] API listuje tylko media pod `/media`; klient używa względnego identyfikatora.
  `resolve()` i sprawdzenie containment po symlinkach; zakaz dowolnej ścieżki.
- [ ] Import rejestruje referencję bez kopii; jawnie obsłuż brak pliku po
  odmontowaniu. Stare uploady pozostają zarządzanymi kopiami, nie zmieniaj ich.
- [ ] Testy traversal, symlink escape, unreadable/missing file, format, brak audio.
  Review bezpieczeństwa przez osobnego workera przed integracją.
- [ ] Rozbuduj historię, render progress i reset pojedynczej sekcji na bazie
  istniejących jobs/edits. Dedykowany stan renderu wymaga osobnego małego PR.

**Akceptacja:** wybór w UI bez transferu i kopiowania GB; brak dostępu poza root.
Później: export presets, segmenty twarz/terminal/twarz, keyframe timeline.
Nie budujemy pełnego NLE, 30 presetów ani obowiązkowego nowego serwisu.

## Kontrakt każdej paczki

Worker czyta AGENTS i handoff; dostaje tylko wymienione pliki i zależności.
Zwraca podsumowanie, diff/pliki, realne komendy/wyniki, ryzyka, session ID
i usage jeśli dostępne. Bez dalszych agentów, sekretów, commit/push, instalacji.
Integrator ogląda cały diff, uruchamia testy z TESTING i zapisuje checkpoint.
Status „ukończone” dopiero po akceptacji; niesprawdzony render pozostaje jawny.
