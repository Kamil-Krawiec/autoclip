# AutoClip — handoff

Stan: 2026-10-25. Baza upstream `5d0eac36fa615b79dd2104083bf273a96f8d68bb`.
Fork: https://github.com/Kamil-Krawiec/autoclip (potwierdzony GitHub API).
Origin = fork, upstream = artbyjazi/autoclip. Lokalna gałąź:
`docs/editor-orchestration-plan`. Nie opublikowano nowych commitów.

## Zakres i decyzje

Użytkownik prosi o orkiestrację i plan; załączony brief obejmuje dalszą
implementację. Priorytety: edycja wygenerowanych klipów, powrót do źródeł,
Fit + blur, manual crop/zoom, upload, napisy. Plan jest w ROADMAP, stan kodu
w FEATURES, komendy w TESTING. Nie zmieniono działania aplikacji w tym etapie.

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
> i zadanie M1 w docs/ROADMAP.md. Zaimplementuj wyłącznie ponowne użycie
> zapisanych źródeł: lista Existing source, Open clips, Process again.
> Użyj istniejących listSources/createJob; nowy job nie usuwa poprzednich
> klipów/edycji. Zachowaj lokalne zmiany Docker i prywatne docker/media.
> Preferuj jednego Argona przez agy, bez dalszej delegacji i płatnego API.
> Przygotuj test reprocess/source preservation, uruchom testy API/DB i build,
> a następnie sprawdź UI. Nie deklaruj sprawdzeń niewykonanych. Nie commituj
> cudzych zmian. Zaktualizuj handoff o diff, wyniki, ograniczenia i koszt.
