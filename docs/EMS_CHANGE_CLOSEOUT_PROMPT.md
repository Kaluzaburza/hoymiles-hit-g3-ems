# Prompt zamknięcia i uporządkowania zadania EMS

Wklej poniższy blok na końcu dowolnego zadania EMS. Dotyczy wszystkich wersji.
Przed następnym zadaniem wskaż agentowi bieżącą gałąź i WORK_STATE integracji.

```text
Zamknij i uporządkuj zmianę wykonaną w tym zadaniu EMS. Wykonaj lokalną
weryfikację, zakresowe commity i integrację zaakceptowanego rozwiązania.
Nie rozpoczynaj kolejnej funkcji. Uwzględnij wszystkie moje decyzje z tej sesji.

1. Ustal aktualne repozytorium, AGENTS.md, gałąź, HEAD/tree, status indeksu
   i worktree oraz obowiązujący docs/WORK_STATE.MD. Znajdź wskazaną tam gałąź
   integracyjną dla tej linii produktu. Sprawdź historię i świeży stan zamiast
   przyjmować, że nazwa wersji lub stary raport identyfikuje aktualny kod.
   Jeżeli cel integracji jest niejednoznaczny, przygotuj wynik na osobnej
   gałęzi i zapytaj o wybór przed przesunięciem niewłaściwej linii.

2. Przejrzyj pełny diff i nowe pliki. Każdą zmianę zaklasyfikuj: wykonana
   w tym zadaniu, odziedziczona i zaakceptowana, obca/niezakończona albo
   odrzucony eksperyment. Zachowaj cudzą pracę i AGENTS.md. Nie stosuj
   zbiorczego git add -A, reset --hard, git clean ani force. Stash nie jest
   trwałym archiwum. Zabezpieczaj potrzebne pliki poza worktree i sprawdzaj
   sumy; archiwalny commit WIP wyraźnie oznacz jako niedopuszczony do wdrożenia.
   Nie dodawaj do Git sekretów, baz HA, .storage, prywatnych logów ani backupów.

3. Uzgodnij zmianę z najnowszą gałęzią integracyjną. Przenieś tylko brakujące
   hunki i ich zależności; nie zastępuj nowszych modułów starym całym plikiem.
   Rozwiąż konflikty na podstawie kontraktów i testów. Zachowaj wcześniejsze
   poprawki bezpieczeństwa, sterowania, historii, LOAD i UI. Odrzuconego kodu
   nie przywracaj. Port pomiędzy wersjami wykonuj wyłącznie w uzgodnionym
   zakresie; poza nim zapisz PENDING lub NOT_APPLICABLE z uzasadnieniem.

4. Zweryfikuj rezultat proporcjonalnie do ryzyka. Dla napraw potwierdź błąd
   i regresję; dla sterowania wymagaj testów właściwych kontraktów. Generowane
   pliki odtwórz z kanonicznych źródeł i porównaj wyniki dwóch przebiegów.
   Po integracji ponów dotknięte testy na końcowym drzewie. Zapisz polecenia,
   wersje środowiska, exit codes, zakres i wyniki. Nie osłabiaj testów ani
   historycznych manifestów dla zielonego wyniku. Niepowiązany błąd opisz
   oddzielnie; nie rozszerzaj automatycznie zadania.

5. Utwórz lokalne, logiczne commity wyłącznie plików należących do rozwiązania
   i jego testów/dokumentacji. Obejrzyj staged diff. Jeżeli istnieją już dobre
   commity, zachowaj je zamiast dublować zmianę. Integrację wykonaj dopiero
   po przeglądzie i wymaganych testach, na czystym i nieużywanym równolegle
   worktree celu. Preferuj fast-forward; gdy historia się rozeszła, uzgodnij
   ją bez przepisywania cudzych commitów i ponów odpowiednie testy.

6. Uaktualnij istniejący WORK_STATE gałęzi integracyjnej jako bieżący punkt
   przekazania. Zapisz: zakres, źródłowe commity, docelową gałąź, zaakceptowane
   i odrzucone warianty, testy, otwarte bramki oraz następny krok. Końcowy SHA
   dokumentacyjnego commita podaj po jego utworzeniu w odpowiedzi/raporcie;
   nie próbuj wpisywać hasha commita do niego samego. Bieżący stan umieść nad
   historią i wyraźnie oznacz stare wpisy. Nie twórz konkurencyjnych handoffów.

7. Rozlicz każdą instalację osobno: pakiet/commit, manifest plików i SHA,
   moment ostatniej weryfikacji, źródło dowodu, odstępstwa hostowe i rollback.
   Odróżnij plik na dysku, załadowany proces HA, aktywny zasób przeglądarki
   i firmware faktycznie wgrany do ESP. Nazwa wersji nie dowodzi zgodności.
   Dane z raportu oznacz datą; nie przedstawiaj ich jako świeżego odczytu.
   Wdrożona wcześniej poprawka może mieć osobny patch/manifest — włącz ją
   do historii Git bez podmieniania hosta starszym pełnym drzewem.

8. Ten prompt upoważnia do lokalnych commitów i integracji. Wdrożenie,
   restart HA, OTA, sterowanie energią, zmiany bazy/retencji, push, tag,
   PR i publikacja wymagają autoryzacji obejmującej te działania w sesji.
   Jeżeli jej nie ma, przygotuj dokładną deltę, backup/rollback i plan testu,
   a stan wdrożenia pozostaw PENDING. Nie pytaj ponownie o już udzieloną zgodę.
   Checkpoint integracyjny nie zamyka freeze ani odbioru terenowego.

9. Posprzątaj wyłącznie własne zakończone artefakty i worktree, kiedy ich
   commity są osiągalne z zachowanej gałęzi, potrzebne pliki są zabezpieczone,
   worktree jest czysty i żadna aktywna sesja go nie używa. Nie usuwaj
   rollbacków potrzebnych do odbioru ani cudzych katalogów. Przy wątpliwości
   pozostaw wpis w inwentarzu zamiast usuwać. Repo źródłowe z obcym WIP może
   pozostać brudne; oficjalny worktree integracji ma być czysty.

10. Zakończ krótkim wynikiem: gałąź/ścieżka i SHA do dalszej pracy, co włączono,
    co pozostało w archiwum, wyniki testów, status worktree, stan każdego hosta,
    otwarte bramki i jeden następny krok. Statusy IMPLEMENTED, OFFLINE,
    INTEGRATED, DEPLOYED, FIELD_ACCEPTED i RELEASE podaj oddzielnie.
    Brak pełnego testu fizycznego nie oznacza braku kodu; obecność kodu nie
    oznacza odbioru. Nie zaczynaj następnego zadania automatycznie.
```

## Cykl dalszej pracy

Jedna poprawka powstaje na osobnej gałęzi/worktree z aktualnej integracji.
Po jej zamknięciu kolejna zaczyna od nowego HEAD integracji. Przy równoległej
pracy integruje jeden prowadzący, a następna zmiana uwzględnia poprzednią.
Gałąź release wskazuje formalny freeze i jest przesuwana w osobnym odbiorze.
Raporty prywatne i środowiska testowe przechowuj poza worktree produktu.
