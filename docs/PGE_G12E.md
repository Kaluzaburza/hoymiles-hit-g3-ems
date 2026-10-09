# PGE G12e — ładowanie taryfowe

Profil `PGE / G12e` używa godzin miesięcznych opublikowanych przez PGE
Dystrybucja dla oddziałów Białystok, Lublin, Łódź, Rzeszów,
Skarżysko-Kamienna, Warszawa i Zamość. Jest oddzielnym profilem od G12 i G12w.
Dodanie opcji do pakietu nie zmienia wybranej taryfy ani zgód EMS.

| Miesiące | Tania strefa w dni robocze |
| --- | --- |
| Styczeń, luty, listopad, grudzień | 13:00–15:00 oraz 22:00–06:00 |
| Marzec, październik | 11:00–15:00 oraz 22:00–06:00 |
| Kwiecień, wrzesień | 10:00–17:00 oraz 22:00–06:00 |
| Maj–sierpień | 09:00–17:00 oraz 22:00–06:00 |

Soboty, niedziele i ustawowe święta są w całości tanie. Końce przedziałów
są wyłączne. Harmonogram używa czasu lokalnego Home Assistant
(`Europe/Warsaw` dla tej taryfy), także przy zmianie czasu i miesiąca.
Założenie: licznik LZO utrzymuje opublikowane godziny stref przy zmianie czasu.
Taryfa OSD dopuszcza także zegar zimowy; inny sposób zaprogramowania licznika
wymaga dopasowania ustawień zamiast bezwarunkowego użycia tego profilu.
Poza wymienionym obszarem nie należy zakładać tego samego harmonogramu.

## Stawki i ich ważność

Profil zakłada Cennik Podstawowy PGE Obrót dla G12e. Stawki krańcowe brutto
obejmują energię z akcyzą i VAT oraz zmienne opłaty dystrybucyjne. Nie obejmują
stałych opłat handlowych, sieciowych, abonamentowych i mocowych.

| Składnik (zł/kWh) | Tania strefa | Droga strefa |
| --- | ---: | ---: |
| Energia brutto | 0,5005 | 0,8363 |
| Sieciowa zmienna netto | 0,0349 | 0,3851 |
| Jakościowa netto | 0,0332 | 0,0332 |
| OZE netto | 0,0073 | 0,0073 |
| Kogeneracyjna netto | 0,0030 | 0,0030 |
| EMS: energia brutto + dystrybucja netto × 1,23 | **0,5969** | **1,3635** |

Stawki EMS są zaokrąglone do czterech miejsc. VAT nie jest doliczany drugi raz
do energii brutto. Referencja porównawcza G11 pozostaje dotychczasową wartością
PGE 1,0991 zł/kWh. Źródła i okres obowiązywania są publikowane w metadanych
profilu. Zestaw `2026.2` jest zweryfikowany na **01.02–31.12.2026**; brak
ważnego cennika blokuje automatyczny profil, zamiast korzystać z cen ręcznych.
Godziny stycznia są zdefiniowane, ale cena styczniowa nie jest tu kwalifikowana.

Wybór G12e z innym automatycznym operatorem jest nieobsługiwany. Tryb `Manual`
pozostaje ręcznym harmonogramem i ręcznymi cenami. Inna umowa sprzedaży niż
Cennik Podstawowy wymaga użycia odpowiednich cen ręcznych. Profil nie zmienia
zasad publicznych godzinowych cen netto Pstryk.

## Źródła sprawdzone 3 października 2026

- [PGE Dystrybucja — taryfa elastyczna i miesięczne godziny G12e](https://pgedystrybucja.pl/uslugi-dystrybucyjne/taryfa-i-cenniki/taryfa-elastyczna).
- [PGE Obrót — Cennik Podstawowy G12e od 01.01.2026](https://www.gkpge.pl/content/download/33731b58d959b9d5a7df60579844f80e/file/cennik-podstawowy-g12e.pdf?inLanguage=pol-PL&version=1&contentId=197134), tabela 1, strona 3.
- [PGE Dystrybucja — wyciąg z taryfy od 01.02.2026](https://www.gkpge.pl/content/download/9d2e5e9068ef4f63ff716a5562d8df20/file/a5_wyciag_z_taryfy_osd_02_2026_web.pdf?inLanguage=pol-PL&version=3&contentId=197491), zasady czasu §2.2 oraz składniki ceny §§7.9–7.12, strona 9.

Testy obejmują wszystkie miesiące i granice stref, święta/weekendy, doby
23/25 h, zmianę miesiąca w cenniku, utratę ważności, niedozwolonego operatora,
rzeczywisty plan ładowania i zgodność taryfy w kontroli stabilizacji RCE.
