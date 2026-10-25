# AutoClip — instrukcje dla agentów

## Cel i zasady

Rozwijamy fork AutoClip: upload → selekcja AI → review → edycja → preview → eksport.
Najważniejsze są edycja istniejących Shortsów, ponowne użycie materiałów i reframe
16:9 → 9:16. Zachowaj silnik, mechanizm selekcji, historię i licencję MIT.
Docelowo Ubuntu Server, Docker Compose, CPU, 32 GB RAM, polskie filmy MOV/MP4.

Stosuj The Pragmatic Programmer (Andrew Hunt, David Thomas): odpowiadaj za wynik,
utrzymuj jedno źródło wiedzy o regułach, niezależne odpowiedzialności, jawne
kontrakty i krótką pętlę sprawdzania. Automatyzuj powtarzalną pracę. Dostarczaj
małe działające przekroje. Prototypy oznaczaj; nie promuj skrótów do produkcji
bez weryfikacji. Naprawiaj drobne problemy w dotykanym obszarze, bez rozszerzania
zakresu. Czytelność i łatwość zmiany mają pierwszeństwo przed rytuałem.

## Mapa i źródła prawdy

- Przed pracą przeczytaj `docs/AGENT-HANDOFF.md`; zadania są w `docs/ROADMAP.md`.
- Przed zmianą pipeline'u czytaj `docs/ARCHITECTURE.md` i jego decyzje projektowe.
- `backend/autoclip/api/`: HTTP i walidacja; `db/`: SQLite, modele i migracje.
- `pipeline/runner.py`: etapy i wznowienia; `pipeline/reframe/`: automatyczny crop.
- `pipeline/captions.py`: presety i ASS; `pipeline/export.py`: FFmpeg.
- `providers/`: adaptery LLM; zachowaj OpenAI-compatible dla DeepSeek.
- `frontend/src/api.ts`: kontrakt klienta; `pages/`: workflow; `components/`: edytor.
- Zakres obecny i planowany: `docs/FEATURES.md`; komendy: `docs/TESTING.md`.

## Uruchamianie i Docker

Używaj Python 3.11/3.12. Instalacja lokalna: `docs/CONTRIBUTING.md`.
Docker: `docker compose -f docker/compose.yaml up --build -d autoclip`.
Sprawdź aktualny port w Compose; podczas audytu CPU używał `127.0.0.1:9879`.
Dane są w named volume `/data`; materiały z hosta w `/media:ro`.
Montowanie `/media` samo nie udostępnia biblioteki w UI.
Zachowaj loopback, lokalne sekrety i brak wymagania GPU/docker.sock.
Nie usuwaj wolumenów ani materiałów użytkownika podczas testów.

## Kontrakty i weryfikacja

Przed edycją znajdź wywołania i odczyty zmienianego kontraktu. Zmiana modelu
obejmuje migrację append-only, DB, Pydantic, klienta TS, zapis/odczyt i eksport.
Zachowaj stare projekty i domyślne zachowanie Auto. Stan zapisanej edycji należy
do `ClipEdit`; pliki automatycznego CropPath są danymi bazowymi, nie drugim
magazynem ręcznych zmian. Nowe ustawienie musi działać po restarcie i w MP4.
Podgląd CSS napisów jest przybliżeniem; nie deklaruj zgodności pikselowej.
Dokładny podgląd potwierdza krótki render tym samym FFmpeg/ASS co eksport.

Nowa nietrywialna logika wymaga małego testu kontraktu. Uruchom właściwe testy
z `docs/TESTING.md`; raportuj realny wynik i pominięcia. Testy nie mogą korzystać
z prawdziwych danych, keyringu ani płatnego API. Dla uploadu waliduj media,
rozmiary i sprzątanie; dla biblioteki również symlinki i wyjście poza katalog.

## Git i delegowanie

Zacznij od `git status --short`, gałęzi, HEAD i remotes. Nie przejmuj cudzych
niezacommitowanych zmian. Nowe zadanie: gałąź od zweryfikowanej bazy; równolegli
workerzy: osobne worktree i rozłączne pliki. Integrator odpowiada za wspólne
modele i lockfile. Zachowaj upstream i małe PR-y. Bez force-push i resetów.

Preferencja użytkownika: Argon przez `agy`; Flash/free OpenCode dla małych zadań.
Łączny limit płatnego DeepSeek to **0,40 USD na cały rozwój opisany w roadmapie**,
nie na sesję ani workera. Przed płatnym wywołaniem sprawdź aktualne stawki,
limit wyjścia, możliwe retry i dotychczasowy koszt w handoffie. Brak pomiaru
kosztu oznacza zatrzymanie płatnej trasy. Dostępność modelu nie dowodzi ceny.
Worker dostaje zakres plików, kryteria, komendy i zakaz dalszej delegacji,
commitów, pushów, instalacji i dostępu do sekretów. Weryfikuj jego wynik.

Ukończone zadanie: działający przekrój UI/API/zapis/render, odpowiednie testy,
brak regresji starego workflow, sprawdzony diff, aktualny handoff i jawne
ograniczenia. Nie nazywaj planu ani pustego wyniku workera implementacją.
