# AutoClip — handoff

Stan: 2026-10-25. Baza upstream `5d0eac36fa615b79dd2104083bf273a96f8d68bb`.
Fork: https://github.com/Kamil-Krawiec/autoclip (potwierdzony GitHub API).
Origin = fork, upstream = artbyjazi/autoclip. Lokalna gałąź: `feat/source-reuse`.
Fundament dokumentacji zapisany w `d62f0f3`, implementacja M1 w `f974fb1`.
Gałąź wypchnięta do forka; PR: https://github.com/Kamil-Krawiec/autoclip/pull/1.
Merge do main zatwierdzony przez użytkownika wyłącznie po zielonych testach PR.
Obecnie oczekujemy na CI; nie wykonano merge. W nowym forku Actions wymagało
ponownego zapisania istniejącego ustawienia enabled=true, aby zarejestrować CI.
Nie rozszerzono uprawnień GITHUB_TOKEN (pozostają read-only).
Checkpoint z patchami, raportami i screenshotem:
`~/.local/state/cost-orchestrator/autoclip-639be96f97/20261025-m1-source-reuse/`.

## Zakres i decyzje

Użytkownik zatwierdził inkrementalny development z agentami, Gitem i zapisem
postępu do Markdown. Priorytety: edycja wygenerowanych klipów, powrót do źródeł,
Fit + blur, manual crop/zoom, upload, napisy. Plan jest w ROADMAP, stan kodu
w FEATURES, komendy w TESTING. Wdrożono zakres M1; dalsze etapy czekają.

Nie ma potrzeby nowego silnika renderu ani osobnej tabeli projects na start.
Używamy istniejących sources/jobs/ClipEdit/CropPath i FFmpeg. Nowa analiza
tworzy nowy job; sama edycja i re-export nie potrzebują LLM.

## Zmiany użytkownika — zachowaj

- `docker/Dockerfile`: libegl1, libgles2 i `pip install . "av<19"`.
- `docker/compose.yaml`: port CPU 9879 zamiast 8000.
- Nieśledzone `docker/media/`: prywatne materiały, nie dodawaj do Git.

Sprawdzony remote HEAD upstream jest równy lokalnej bazie; lokalne poprawki
Docker nie występują w tym HEAD. Ich skuteczność w świeżo zbudowanym obrazie
pozostaje osobną weryfikacją. Nie commituj ich razem z dokumentacją.

## Routing i pieniądze

### Iteracja M1

| Paczka | Wykonawca | Wynik |
|---|---|---|
| UI Existing source / Process again / Open clips | agy gemini-4-argon-low | Patch tekstowy po odmowie command w headless; zastosowany i zweryfikowany |
| Regresja zachowania starych danych | Codex gpt-6.1-sol medium | Test API/DB, 70 testów PASS w worktree |
| Mała lista przypadków QA | OpenCode opencode/deepseek-v4.1-flash-free | 6 przypadków; eksport sesji raportuje cost=0 |
| Niezależne review M1 | Codex gpt-6-astra medium | Brak usterek wymagających poprawki |
| Browser self-check | Codex gpt-6.1-sol medium | Firefox PASS; osobny review skryptu bez usterek |

Argon: conversation `59b2defa-44a3-4021-9dc9-0679a6663cf6`.
Pierwszy headless call zwrócił pusty response po odmowie narzędzia command;
nie zmieniano allowlist ani nie użyto bypassów. Drugie, tool-free wywołanie
otrzymało zawartość pojedynczego pliku i zwróciło diff. Zastosowanie wymagało
`git apply --recount` z powodu błędnych liczników hunków, bez zmiany treści.
Raport końcowy Argon: input 32722, output 2827, cache_read 14708; pierwsze
wywołanie raportowało input 14707/output 303. Nie sumujemy ich jako potwierdzonej
faktury, bo usage wznowionej rozmowy może obejmować poprzedni kontekst.

OpenCode session `ses_e8b9a3d3dffeX1HdS6Qw52R0BN`, provider opencode,
raportowane input 6551/output 650/reasoning 300/cache read 1536, koszt 0.
Nie było wywołań płatnego providera DeepSeek: wydano 0,00 USD z limitu 0,40 USD.
Koszt AGY/Codex i pozostałe quota/reset nadal nieznane; bez twierdzeń o oszczędności.

Ruling: ta iteracja kończy się na niezależnie testowalnym M1. Współdzielone
modele M2 i framing M3 będą kolejnymi commitami, bez równoległego nakładania
zmian. Koszt tej decyzji: Fit i rozbudowany edytor pozostają na następny etap.

Worktree UI: `/home/kamil/autoclip-worktrees/source-reuse`.
Worktree testów: `/home/kamil/autoclip-worktrees/source-tests`.
Nie usuwaj niezacommitowanych worktree przed zachowaniem patchy/checkpointu.

### Audyt poprzedzający development

Potwierdzone CLI: agy, OpenCode v2.0.32, gh zalogowany jako Kamil-Krawiec.
Katalog agy zawiera gemini-4-argon-low/medium/high i gemini-3.8-flash.
Preferowany worker: Argon. OpenCode pokazuje m.in. deepseek/deepseek-flash
oraz warianty oznaczone free; sam katalog nie potwierdza bieżącej ceny/auth.

Wywołano jeden audyt read-only `agy --model gemini-4-argon-low --mode plan
--sandbox`, z limitem 150 s, bez bypassów permissions. Conversation ID:
`5b35e8ea-7ab2-4425-b8c9-6115135af757`. CLI zwrócił exit 0 i JSON SUCCESS,
ale stderr zgłosił print timeout z częściowym wynikiem i odpowiedź jest ucięta.
Traktuj to jako częściowy audyt, nie ukończony plan workera. Potwierdzone kodem:
fit/object-cover, ratio/cache, trim/crop, edited_words/trim, brudny tekst przy
export i kolizje plików. Nie przyjęto niezweryfikowanych dodatkowych tez.
Usage raportowane: input 87151, output 7659, cache_read 126822; nie przeliczano
tego na cenę ani procent subskrypcji. Nie uruchomiono kolejnego workera.
Nie wykonano wywołań DeepSeek: **koszt płatnego DeepSeek w tym etapie 0,00 USD**.
Pozostaje **0,40 USD na cały zakres**, nie na każdą sesję. Koszt/usage AGY,
limity pięciogodzinne i reset: nieudostępnione przez sprawdzony help, nieznane.
Przed paid wywołaniem potrzebne aktualne stawki, twarde ograniczenie wyjścia,
uwzględnienie retry i dopisanie kosztu do tego rejestru. Bez pomiaru użyj
Argona albo pracy bez płatnego API, nie ryzykuj przekroczenia cap.

## Weryfikacja wykonana

### Aktualna iteracja M1

- `pytest -p no:cacheprovider -m "not slow and not golden and not e2e" -q`:
  **277 passed, 42 deselected**, 9,76 s w efemerycznym kontenerze.
- `ruff check --no-cache backend tests` i format zmienionego testu — PASS.
- `npm run build` — PASS; TypeScript i Vite. Przywróć własny wygenerowany
  `frontend/tsconfig.tsbuildinfo` przed commitem.
- Review niezależnego workera: brak findings w aplikacji/testach API i osobno
  w browser self-check. Test przeglądarkowy Firefox PASS: 1 POST jobs/0 ingest,
  bieżące max_clips=3, stare edycje zachowane, zero page errors. Sprawdzone
  loading/empty/error+retry, Open clips, zwolnienie blokady po błędzie API,
  podwójny submit i drop podczas busy. Screenshot zachowany w checkpoint.
- Backend QA działał pod `127.0.0.1:19879`, worker wyłączony, syntetyczne dane
  w /tmp efemerycznego kontenera; żadnych produkcyjnych wolumenów ani kluczy API.
  Po QA kontener został zatrzymany i usunięty. Nie uruchomiono produkcyjnego UI.
- Open clips przeszukuje tylko 8 ostatnich globalnych jobów; dropdown źródeł
  domyślnie do 50. Dostępność pliku źródłowego na dysku nie jest sprawdzana w UI.
- Nie uruchamiano realnej analizy AI ani renderów: zmiana dotyczy wyboru źródła
  i ochrony zachowania danych. Regresja API działa z wyłączonym workerem.

### Baseline przed developmentem

- 276 testów unit/API przeszło, 42 deselected; efemeryczny kontener istniejącego
  `autoclip:latest`, checkout read-only, pytest 6,92 s, bez realnego API.
- Self-check istniejącego fit filtergraph: boxblur i overlay obecne,
  burn_captions=false nie dodaje ASS — PASS. To nie dowód wizualny.
- Pierwszy Ruff nie mógł zapisać cache w read-only mount; poprawione wywołanie
  `ruff check --no-cache backend tests` — PASS.
- Realne testy renderu: 18 passed, 300 deselected, 65,82 s, ten sam istniejący
  obraz z aktualnym checkoutem i efemerycznymi narzędziami testowymi.
- `npm ci --no-audit --no-fund` i `npm run build` — PASS (TypeScript + Vite).
- `docker compose -f docker/compose.yaml config --quiet` i `git diff --check`
  — PASS. Build zmienił śledzony tsbuildinfo; przywrócono wyłącznie ten własny
  wygenerowany diff. Pozostałe lokalne zmiany użytkownika zachowano.
- Compose nie pokazał uruchomionej usługi AutoClip podczas audytu.
- Nie sprawdzono przeglądarki, uploadu 5 GB, świeżego Docker build, naturalnego
  materiału ani live DeepSeek. Pełny rewrite nie jest ukończony.

## Następny konkretny prompt

> Pracuj w /home/kamil/autoclip. Przeczytaj AGENTS.md, docs/AGENT-HANDOFF.md
> i M2/M3 w docs/ROADMAP.md. M1 jest ukończone — nie implementuj go ponownie.
> Zacznij następny mały przekrój: trwały stan edycji ClipEdit potrzebny do
> wyboru Auto/Fit i napisów ON/OFF, zgodny dla preview i eksportu. Najpierw
> test starej bazy/migracji i częściowego patch bez utraty słów/stylu/ratio.
> Zachowaj lokalne Docker diffy i prywatne media. Preferuj Argona do kodu,
> Codexa do testów/review, małe zadania do OpenCode. Headless agy nie może
> uruchamiać command: w razie odmowy użyj tool-free patch z potrzebnym kodem
> w promptcie, bez bypassów. Nie nadpisuj automatycznego CropPath. Dalsze
> elementy M2 dziel na osobne testowalne commity. Nie wydawaj płatnego API;
> nadal pozostaje 0,40 USD. Zaktualizuj handoff, testy i Git po weryfikacji.
