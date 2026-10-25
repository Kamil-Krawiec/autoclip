# AutoClip — stan funkcji

Audyt 2026-10-25, baza `5d0eac36fa615b79dd2104083bf273a96f8d68bb`.
„Istnieje” oznacza obecność w kodzie; zakres wykonanych testów jest w handoffie.

| Obszar | Istnieje | Brakuje / następna zmiana |
|---|---|---|
| Pipeline | FastAPI, SQLite, kolejka FIFO w procesie, SSE, etapy i artefakty wznowienia | Zachować; nie przepisywać selekcji i transkrypcji |
| Transkrypcja i selekcja | faster-whisper, opcjonalny WhisperX, indeksy słów LLM, refinement granic, adapter OpenAI-compatible | Test live DeepSeek tylko świadomie, poza baseline |
| Upload | `UploadFile`, odczyty po 4 MiB do stagingu, walidacja suffix i ffprobe | Postęp bajtów, cancel, limity dysku/rozmiaru, audyt multipart spooling i sprzątania |
| Istniejące materiały | `GET /api/sources`, `POST /api/jobs` przyjmuje istniejący `source_id` | Wybór źródła w UI i nowa analiza bez ponownego uploadu |
| Historia | SQLite sources/jobs/clips/edits/exports, lista 8 ostatnich zadań w Ingest | Wygodne rozróżnienie „otwórz edycję” i „analizuj ponownie”; później paginacja |
| Biblioteka lokalna | Compose montuje `/media:ro`; CLI potrafi ingest pliku | Bezpieczne API i UI przeglądania; obecny `ingest_file` kopiuje plik |
| Napisy | 4 presety: Bold Pop, Karaoke Fill, Clean Lower, Boxed; korekta słów, zapis stylu i ratio | Parametry wyglądu, drag pozycji, zapis ON/OFF |
| ASS | `CaptionStyle`, animacje none/scale/karaoke, fonty Anton/Inter | Udostępnić kontrolowane nadpisania; nie dublować katalogu presetów |
| Napisy OFF | `pipeline.export.ExportRequest.burn_captions` | Podłączyć do modelu edycji, HTTP, UI i preview |
| Auto reframe | detekcja ujęć/twarzy, tracker, speaker mapping, smoothing, segmenty CropPath i keyframe'y | Zachować jako bazę i reset |
| Fit + blur | `CropSegment.fit` i `_fit_chain` w FFmpeg | Wybór w UI, trwały override, prawidłowy preview |
| Manual crop/zoom | CropPath wyraża okno i pozycję; segment.zoom to istniejący animowany push-in | Statyczny zoom edytora, drag i zapis; nie mylić go z push-in |
| Preview | Oryginalne media z Range, interpolacja crop path, napisy HTML/CSS | Fit obecnie wpada w object-cover; ratio i trim mogą rozmijać cache i render |
| Eksport | CPU FFmpeg/libass, ratio 9:16/1:1/16:9, opcjonalne SRT, rekordy eksportu | Bezpieczne wersje plików, ograniczenie renderów, stan per eksport, dokładny preview |

## Główne ryzyka audytu

1. `_crop_path_for` używa cache, gdy żądane ratio to 9:16, bez odczytu ratio
   bazowego zadania i aktualnych granic. Preview pobiera cache osobno. Potrzebny
   wspólny resolver efektywnego kadru, obsługujący również zmieniony trim.
2. `cropWindowStyle` zwraca null dla fit; JSX wtedy używa object-cover.
3. Nazwa eksportu zależy od tytułu i ratio: powtórny render może nadpisać plik
   wskazywany przez wcześniejsze rekordy. Równoległe żądania nie są kolejką FIFO.
4. React pobiera słowa/kadr bez ochrony przed spóźnioną odpowiedzią po zmianie
   klipu. Niezapisany tekst można utracić podczas przełączenia klipu.
5. Upload ma pętlę odczytu, lecz multipart może wcześniej użyć pliku spool.
   Nie dowodzi to obsługi 5 GB, limitu RAM ani poprawnego cleanup po cancel.
6. `clip_words` zwraca zapisane edited_words bez ponownego ograniczenia do
   trimu. Export w Review nie zapisuje automatycznie brudnego tekstu. Testuj
   korektę → trim → export oraz export bez wcześniejszego Save.

Plan wdrożenia i kryteria są w [ROADMAP.md](ROADMAP.md).
