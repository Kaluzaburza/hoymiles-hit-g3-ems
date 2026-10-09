# Aktualizacja ESP bez ręcznego poprawiania pakietów

Dostępne od **1.5.8.1**. Po aktualizacji przez HACS i restarcie HA otwórz
**Instrukcja i aktualizacja ESP** na stronie Start lub w ustawieniach EMS.
[Przejście z 1.5.7](UPGRADE_1_5_7.md) opisuje także aktualizację pakietów HA.

HACS aktualizuje integrację w Home Assistant. ESPHome Builder osobno kompiluje
i wgrywa oprogramowanie urządzenia ESP. Nie trzeba usuwać integracji, dodawać
urządzenia od nowa ani ręcznie kasować `register_count` w kilkunastu plikach.

## Cztery kroki

1. **Przygotuj instalację.** Zrób kopię Home Assistanta. W EMS użyj „Wstrzymaj”
   i poczekaj, aż bieżące działanie się zakończy. Sprawdź **Ustawienia → System →
   Naprawy**; wykonaj dodatkowy restart tylko wtedy, gdy HA go wymaga po instalacji
   pakietów. ESPHome Device Builder musi mieć wersję co najmniej **2026.9.0**.
   Podczas pracy wyspowej nie wymuszaj innego trybu falownika.
2. **Otwórz instrukcję w EMS.** Na stronie Start lub w ustawieniach wybierz
   **Instrukcja i aktualizacja ESP**. W Builderze otwórz swoje urządzenie Hoymiles
   → **Edytuj**, skopiuj cały YAML i wklej do kreatora. Możesz też wczytać jego
   zapisany plik. Użyj własnego pliku urządzenia, nie przykładu z internetu.
3. **Wklej początek logu urządzenia i przygotuj plik.** W Builderze wybierz
   **Logi**. Skopiuj linie z `[app:…]: ESPHome version …` i, jeśli jest dostępna,
   `[esphome.ota:…]: Encryption: …`. Wersja Buildera z nagłówka `INFO ESPHome …`
   nie jest wersją działającego urządzenia. Kliknij **Przygotuj plik**, pobierz
   kopię oryginału, a następnie przygotowany YAML.
4. **Wgraj w Builderze.** Skopiuj przygotowany YAML do edytora **tego samego
   urządzenia**. Zapisz, wybierz **Zweryfikuj**, a po poprawnym wyniku
   **Zainstaluj/Aktualizuj → bezprzewodowo**. Postępuj według pokazanego etapu OTA.
   Po końcowym wgraniu sprawdź świeże dane i gotowość EMS. Dopiero wtedy przywróć
   wcześniej używane automatyki.

Instrukcja i changelog pozostają dostępne także po aktualizacji HACS. Przycisk
„Znam instrukcję” tylko zwija przypomnienie w tej przeglądarce — nie potwierdza
wgrania ani sprawności urządzenia.

## Dlaczego czasem są dwa etapy OTA?

Starszy firmware, np. ESPHome 2026.8.2, nie przyjmie od razu wymaganego szyfrowanego
OTA. Kreator najpierw zachowuje dotychczasowe hasło i klucz API. Ostrzeżenie
Buildera o koszcie hasła OTA jest na tym przejściowym etapie oczekiwane.

Po pierwszym wgraniu wróć do kreatora z **właśnie zapisanym YAML-em i nowym logiem**.
Gdy log pokazuje `Encryption: offered, plaintext accepted`, przygotuj i wgraj
drugi plik. Na końcu oczekiwany jest komunikat `Encryption: required`.
Nie twórz nowego klucza ani nowego hasła. Opis techniczny:
[ESPHome: enabling encryption on an existing device](https://esphome.io/components/ota/esphome/#enabling-encryption-on-an-existing-device).

## Jeśli pojawi się prośba o pakiety

Starsze instalacje mogą używać `!include packages/…`. Kreator poprosi wtedy
o wskazane pliki z kopii folderu `packages` na komputerze. Porówna ich treść
z rozpoznanymi oficjalnymi wersjami. Własne zmiany lub nieznana konfiguracja
wymagają przeglądu — kreator nie zastępuje ich w ciemno. Nie wybieraj `secrets.yaml`.
Jeśli nie masz kopii tych plików, poproś osobę obsługującą Twoją instalację
o pomoc w ich skopiowaniu; nie kasuj folderu `packages`.

## Co jest zachowywane i gdzie trafiają dane?

Zachowane są nazwa urządzenia, ustawienia płytki i pamięci, piny, parametry RS485,
częstotliwości odczytów, ustawienia użytkownika i odwołania do istniejących kluczy.
Zmieniane są oficjalny zestaw pakietów, wskazanie źródła konfiguracji i transport
OTA. Pakiety są przypięte do pełnego SHA zgodnego firmware'u, nie do ruchomego `main`.

Przygotowanie wymaga administratora HA. YAML jest przetwarzany przez **Twój HA**;
nie wysyłamy go do autora ani GitHuba. Log zostaje w przeglądarce. Backend nie
zapisuje plików konfiguracyjnych, nie łączy się z ESP i nie wykonuje OTA, restartu
ani sterowania. „Plik przygotowany” nie zastępuje weryfikacji i kompilacji Buildera.

Samo promowanie zgodnego firmware'u RC2 do stabilnej integracji 1.5.8 nie wymaga
ponownego OTA. Numer projektu `1.5.8rc2` jest w tym przypadku prawidłowy.

## English

Available in **1.5.8.1**. After installing the HACS update and restarting
HA, open **Instructions and ESP update** on Overview or Settings. Back up HA,
pause EMS and finish active execution. Keep the existing device/integration.
Use ESPHome Builder 2026.9.0 or newer. Load the actual device YAML and running
device boot log, prepare the file, download the backup first, then validate and
upload through Builder. Older firmware needs a password-preserving bridge before
the final encrypted OTA step; return with the new log between stages. Unknown
or modified configurations stop for review. Processing stays on your HA; the
wizard neither uploads firmware nor controls the inverter.
