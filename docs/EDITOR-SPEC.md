# AutoClip — zakres rozwoju edytora

Wymagania użytkownika z 2026-10-25, uzupełniające obecny AutoClip.
Nie jest to deklaracja istniejących funkcji; stan jest w FEATURES.

## Rezultat

Upload → AI selection → Review → Edit → Preview → Export. Autor Kod Zero ma
w kilka minut poprawić propozycję AI i wrócić później do zapisanych edycji.
Zachowujemy działającą selekcję momentów, transkrypcję i refinement granic.

## Priorytety

1. Edycja wygenerowanych Shortsów i trwałe projekty: zapis tekstu/wyglądu/kadru,
   powrót po zamknięciu przeglądarki, ponowny eksport bez LLM.
2. Wybór istniejącego źródła do nowej analizy bez ponownego przesyłania;
   zachowanie poprzednich zadań, klipów i ich edycji.
3. Reframe poziomego filmu do pionu: Auto, całe źródło na powiększonym rozmytym
   tle, ręczny crop, statyczny zoom i pozycja, Reset Auto i porównanie.
   Terminal/przeglądarka muszą pozostać czytelne. Screen Focus wykorzystuje
   istniejącą geometrię. Opcjonalna safe-area nie trafia do eksportu.
4. Upload MOV/MP4 2–5 GB: rzeczywiste bajty/%, prędkość, wiarygodne ETA,
   cancel i błędy. Transfer, walidacja i analiza mają osobne stany.
5. Caption Studio: zachować presety; font, rozmiar, tekst/aktywny kolor, obrys,
   cień, podświetlenie, słowa w linii, pozycja/marginesy, drag, ON/OFF i korekta
   tekstu. Kilka dopracowanych stylów, nie katalog kosmetycznych duplikatów.

## Kontrakty

- Zapisana edycja jest źródłem ustawień preview i MP4; jeden model ClipEdit.
- Preview interaktywny może przybliżać libass, dokładny proof używa produkcyjnego
  renderera. Użytkownik musi wiedzieć, który efekt ogląda.
- Edycje jednego klipu nie wpływają na inne; stare dane pozostają czytelne.
- Dane i sekrety lokalne, bez logowania kluczy i zbędnie publicznych usług.
- Host: Dell, 32 GB RAM, Ubuntu Server, CPU, Docker Compose i SSH forwarding.
- Typowe filmy polskie 15–30 min, Full HD 30/60 FPS, A-roll i screencasty.
- Biblioteka z read-only mount, bez traversal i zbędnego kopiowania GB.
- Lokalne problemy PyAV/metadata_errors i MediaPipe/libEGL wymagają sprawdzenia
  na świeżym obrazie; nie zakładamy, że upstream zawiera lokalne poprawki.

## Granice i sposób pracy

Małe niezależne etapy i PR-y, testy mock/małe media, realny FFmpeg tam gdzie
chroni wynik. Brak pełnego NLE i migracji na inny silnik Shortsów. Keyframe
timeline jest etapem późniejszym. Tus oceniamy po pomiarze potrzeb, bez własnego
protokołu chunkowania. Nie dodajemy obowiązkowego GPU ani AI super-resolution.

Preferowany wykonawca Argon przez agy; Flash/OpenCode do ograniczonych zadań.
Cały rozwój ma wspólny limit płatnego DeepSeek **0,40 USD**. Audyt, plan i
testy nie wymagają płatnego API. Przed wywołaniem płatnym konieczny pomiar
stawki, limit wyjścia, rezerwa retry i aktualizacja rejestru kosztów w handoffie.
