// Bundled locally with HACS. No CDN, external requests or device commands.
export function classifyOtaLog(log) {
  const text = String(log || "").replace(/\x1b\[[0-9;]*m/g, "");
  // The Builder's "INFO ESPHome ..." is the compiler, not the running device.
  const versions = [...text.matchAll(/\[app:\d+\]:\s*ESPHome version (\d+)\.(\d+)\.(\d+)/g)];
  const unique = new Set(versions.map((m) => `${m[1]}.${m[2]}.${m[3]}`));
  if (unique.size !== 1) return "unknown";
  const m = versions[0];
  if (Number(m[1]) < 2026 || (Number(m[1]) === 2026 && Number(m[2]) < 9)) return "legacy";
  return /\[esphome\.ota:\d+\]:\s*Encryption:\s*(required|offered, plaintext accepted)\s*$/m.test(text)
    ? "encrypted" : "unknown";
}

const copy = {
  pl: {
    title: "Aktualizacja EMS i ESP", close: "Zamknij", intro: "HACS zaktualizował część działającą w Home Assistant. ESP to osobne urządzenie — może wymagać drugiego kroku.",
    newsTitle: "Co zmieniło się w EMS 1.5.8.1?", news: "Instrukcja pozostaje dostępna po aktualizacji HACS. Kreator przygotuje YAML ESP z Twojej konfiguracji, zachowa ustawienia i wyjaśni kolejne kroki aktualizacji.",
    notes: "Pełna lista zmian", steps: "Instrukcja pozostaje tutaj także po aktualizacji HACS.",
    current: "Masz już zgodny firmware 1.5.8rc2 z protokołem lease 2? Ta aktualizacja EMS do 1.5.8.1 nie wymaga ponownego wgrywania ESP. Sama nazwa „1.5.8” w starym logu nie potwierdza zgodności.",
    before: "1. Zrób kopię i zakończ pracę EMS", beforeText: "Zrób kopię Home Assistanta. W EMS użyj „Wstrzymaj” i zaczekaj na zakończenie aktywnego działania. Nie zmieniaj trybu falownika podczas pracy wyspowej. Kreator sam niczego nie zatrzymuje ani nie wgrywa.",
    ha: "Sprawdź Ustawienia → System → Naprawy. Jeżeli HA prosi o kolejny restart po instalacji pakietów, wykonaj go. Zaktualizuj ESPHome Device Builder do wersji co najmniej 2026.9.0. Nie usuwaj integracji ani urządzenia ESPHome.",
    yamlTitle: "2. Wczytaj obecny plik urządzenia", yamlHelp: "W ESPHome Device Builder otwórz urządzenie Hoymiles → Edytuj. Skopiuj cały YAML i wklej poniżej albo wybierz jego zapisany plik. To ma być Twój obecny plik, nie przykład z internetu.",
    sourceLabel: "Obecny YAML urządzenia", fileLabel: "Wybierz plik YAML", privacy: "Plik jest przetwarzany wyłącznie przez Twój Home Assistant. Nie trafia do GitHuba ani autora integracji. Nie wczytuj secrets.yaml. Kopię i przygotowany plik przechowuj prywatnie.",
    logTitle: "3. Sprawdź wersję działającego ESP", logHelp: "W ESPHome Builder otwórz Logi urządzenia. Wklej początek logu z linią „[app:…]: ESPHome version …” oraz, jeśli jest dostępna, „[esphome.ota:…]: Encryption: …”. Nie używaj wersji Buildera z nagłówka. Log pozostaje w przeglądarce; wysyłamy tylko rozpoznany rodzaj aktualizacji.",
    logLabel: "Początek logu urządzenia", unknown: "Nie rozpoznano możliwości OTA. Wklej log działającego urządzenia. Jeśli tych linii nie ma, skorzystaj z instrukcji lub pomocy — nie wybieramy sposobu wgrywania na zgadywanie.",
    legacy: "Rozpoznano starszy firmware. Przygotujemy etap 1 z zachowaniem dotychczasowego hasła OTA.", encrypted: "Log wskazuje obsługę szyfrowanego OTA. Przygotujemy plik końcowy z obecnym kluczem API.",
    prepare: "Przygotuj plik", busy: "Sprawdzanie konfiguracji…", admin: "Przygotowanie pliku wymaga konta administratora Home Assistanta.",
    localTitle: "Potrzebne są stare pakiety", localText: "Ten YAML korzysta z lokalnego folderu packages. Wybierz wskazane pliki z jego kopii na komputerze, aby sprawdzić, czy nie zawierają własnych zmian. Nie wybieraj secrets.yaml. Pliki na urządzeniu pozostają bez zmian.",
    packagesLabel: "Wybierz pliki z folderu packages", retry: "Sprawdź ponownie", ready: "Plik przygotowany — jeszcze nie wgrany", backup: "Najpierw pobierz kopię oryginału", download: "Pobierz przygotowany YAML", prepared: "Zachowano nazwę urządzenia, płytkę, piny, ustawienia i odwołania do kluczy. Zaktualizowano oficjalne pakiety oraz ustawienia transportu OTA.",
    install: "4. Wgraj przez ESPHome Builder", installText: "Otwórz pobrany YAML, skopiuj całość do edytora TEGO SAMEGO urządzenia w Builderze i zapisz. Użyj „Zweryfikuj”, a dopiero po poprawnym wyniku „Zainstaluj”/„Aktualizuj” → bezprzewodowo. Nie twórz nowego urządzenia i nie zmieniaj klucza API. Komunikat kreatora nie zastępuje walidacji ani kompilacji ESPHome.",
    bridge: "To dopiero etap 1. Ostrzeżenie o haśle OTA jest na tym etapie oczekiwane. Po wgraniu wróć tutaj, wczytaj właśnie zapisany YAML i nowy log. Gdy log pokaże „Encryption: offered, plaintext accepted”, kreator przygotuje etap 2. Nie pomijaj drugiego etapu.",
    final: "Po wgraniu sprawdź w nowym logu „Encryption: required”. W HA sprawdź aktualne dane, gotowość EMS i Naprawy. Przywróć tylko wcześniej używane automatyki, gdy odczyty są świeże, a sterownik zakończył poprzednią transakcję.",
    failed: "Nie przygotowano pliku. Obecna konfiguracja nie została zmieniona.", support: "Instrukcja i pomoc", tooLarge: "Plik jest zbyt duży. Wybierz YAML urządzenia, nie pełny pakiet diagnostyczny.", network: "Nie udało się połączyć z Home Assistantem. Sprawdź połączenie i spróbuj ponownie.",
    read: "Znam instrukcję — zwiń przypomnienie", details: "Szczegóły techniczne", backupFirst: "Pobierz kopię przed plikiem do aktualizacji.", noSecrets: "Nie wybieraj secrets.yaml. Potrzebny jest YAML urządzenia z Buildera.", fileRead: "Nie udało się odczytać pliku. Wybierz go ponownie lub wklej YAML z edytora.",
    errors: {
      transport_unknown: "Brakuje potwierdzenia sposobu OTA. Sprawdź log działającego ESP.",
      ota_password_missing: "Starsze OTA wymaga dotychczasowego hasła. Nie ma go ani odwołania do niego w tym YAML-u. Nie generuj nowego; odzyskaj istniejącą konfigurację lub poproś o pomoc.",
      modified_packages: "Pakiety zawierają własne zmiany lub nieznaną wersję. Zachowaj kopię i poproś o sprawdzenie różnic. Nie nadpisujemy ich automatycznie.",
      incomplete_packages: "Wybierz wszystkie wskazane pliki packages, bez duplikatów i bez secrets.yaml.",
      unsupported_packages: "To nie jest rozpoznany kompletny zestaw oficjalnych pakietów. Potrzebny jest przegląd konfiguracji.",
      custom_configuration: "Ten YAML zawiera własne dodatki. Potrzebny jest przegląd, aby ich nie utracić.",
      unknown_board: "Nie rozpoznano obsługiwanej płytki. Nie zamieniaj jej na inną — poproś o sprawdzenie konfiguracji.",
      custom_ota: "Ustawienia OTA są niestandardowe. Zachowaj działający plik i skorzystaj z pomocy.",
      custom_api_key: "Klucz API ma niestandardowe ustawienia. Nie zmieniaj klucza; potrzebny jest przegląd konfiguracji.",
      missing_existing_settings: "Brakuje ustawień obecnego urządzenia. Wklej cały YAML; nie podawaj samego fragmentu.",
      unsupported_yaml: "Nie udało się jednoznacznie odczytać YAML-a. Wklej cały niezmieniony plik urządzenia z Buildera.",
      source_too_large: "Plik urządzenia przekracza dozwolony rozmiar.", package_too_large: "Wybrany pakiet jest zbyt duży.",
    },
  },
  en: {
    title: "Update EMS and ESP", close: "Close", intro: "HACS updated the Home Assistant integration. ESP is a separate device and may need a second step.",
    newsTitle: "What changed in EMS 1.5.8.1?", news: "Instructions remain available after the HACS update. The wizard prepares ESP YAML from your configuration, preserves settings and explains each update step.", notes: "Full release notes", steps: "These instructions remain available after the HACS update.",
    current: "Already using the compatible 1.5.8rc2 firmware with lease protocol 2? This EMS 1.5.8.1 update alone does not require another ESP upload. An old log labelled “1.5.8” alone does not prove compatibility.",
    before: "1. Back up and finish EMS activity", beforeText: "Back up Home Assistant. Use Pause in EMS and wait for active execution to finish. Do not force a different inverter mode during island operation. This guide does not stop or upload anything.", ha: "Check Settings → System → Repairs. Restart again if HA requests it after installing the packages. Update ESPHome Device Builder to at least 2026.9.0. Keep the existing integration and ESPHome device.",
    yamlTitle: "2. Load your current device file", yamlHelp: "In ESPHome Device Builder, open the Hoymiles device → Edit. Copy the entire YAML below or select your saved file. Use your actual configuration, not an example from the internet.", sourceLabel: "Current device YAML", fileLabel: "Choose YAML file", privacy: "Only your Home Assistant processes this file. It is not sent to GitHub or the integration author. Do not load secrets.yaml. Keep the backup and generated file private.",
    logTitle: "3. Check the running ESP version", logHelp: "Open the device Logs in ESPHome Builder. Paste the beginning with “[app:…]: ESPHome version …” and, when available, “[esphome.ota:…]: Encryption: …”. The Builder header version is not the device version. The log stays in this browser; only the detected transport is sent.", logLabel: "Beginning of device log", unknown: "OTA capabilities are unknown. Paste the running device log. If those lines are unavailable, use the guide or ask for help; the upload method will not be guessed.", legacy: "Older firmware detected. Stage 1 will keep the existing OTA password.", encrypted: "The log indicates encrypted OTA support. The final file will use the existing API key.",
    prepare: "Prepare file", busy: "Checking configuration…", admin: "Preparing a file requires a Home Assistant administrator account.", localTitle: "Old packages are needed", localText: "This YAML uses a local packages folder. Select the listed files from your backup to check for custom changes. Do not select secrets.yaml. Files on the device stay unchanged.", packagesLabel: "Select files from packages", retry: "Check again", ready: "File prepared — not uploaded", backup: "Download original backup first", download: "Download prepared YAML", prepared: "Device name, board, pins, settings and key references are preserved. Official packages and OTA transport settings were updated.",
    install: "4. Upload using ESPHome Builder", installText: "Open the downloaded YAML and copy all of it into the SAME device editor in Builder, then save. Choose Validate and only after success choose Install/Update → wirelessly. Do not create another device or change the API key. This guide does not replace ESPHome validation or compilation.", bridge: "This is stage 1 only. The OTA password warning is expected here. After uploading, return, load the YAML you just saved and paste the new log. Once it shows “Encryption: offered, plaintext accepted”, the guide can prepare stage 2. Do not skip stage 2.", final: "After uploading, check the new log for “Encryption: required”. In HA check fresh telemetry, EMS readiness and Repairs. Restore only your previous automations after fresh readback and completion of the previous transaction.",
    failed: "No file prepared. Your current configuration has not been changed.", support: "Instructions and support", tooLarge: "File too large. Select the device YAML, not a diagnostic archive.", network: "Could not connect to Home Assistant. Check your connection and try again.", read: "I have read this — collapse reminder", details: "Technical details", backupFirst: "Download the backup before the update file.", noSecrets: "Do not select secrets.yaml. Load the device YAML from Builder.", fileRead: "Could not read this file. Select it again or paste YAML from the editor.",
    errors: {
      transport_unknown: "OTA transport is unconfirmed. Check the running device log.", ota_password_missing: "Older OTA needs the existing password, which is not present or referenced in this YAML. Do not create a new one; recover the existing configuration or ask for help.", modified_packages: "Packages contain custom changes or an unknown version. Keep a backup and request a comparison; they will not be overwritten automatically.", incomplete_packages: "Select all listed package files, with no duplicates or secrets.yaml.", unsupported_packages: "This is not a recognized complete set of official packages. Configuration review is needed.", custom_configuration: "This YAML contains custom additions. Review is needed to preserve them.", unknown_board: "The board is not recognized. Do not replace it with another board; request a configuration review.", custom_ota: "OTA settings are customized. Keep the working configuration and ask for help.", custom_api_key: "The API key uses custom settings. Keep the key; configuration review is needed.", missing_existing_settings: "Existing device settings are missing. Paste the entire device YAML, not a fragment.", unsupported_yaml: "YAML could not be read unambiguously. Paste the complete unmodified device file from Builder.", source_too_large: "The device file exceeds the size limit.", package_too_large: "A selected package is too large.",
    },
  },
};

export function openHoymilesUpdateGuide(hass, language = "en") {
  const t = copy[language === "pl" ? "pl" : "en"];
  const active = document.querySelector("[data-hoymiles-update-dialog]");
  if (active) { active.focus(); return active; }
  const dialog = document.createElement("dialog");
  dialog.dataset.hoymilesUpdateDialog = "";
  dialog.setAttribute("aria-label", t.title);
  // Constant/localized markup only. YAML, filenames, logs and server responses
  // are inserted with value/textContent, never into HTML.
  dialog.innerHTML = `<style>
    [data-hoymiles-update-dialog]{box-sizing:border-box;width:min(790px,calc(100% - 24px));max-height:90vh;padding:0;border:1px solid #365568;border-radius:18px;background:#101e28;color:#e5f1f7;font:15px/1.55 system-ui,sans-serif;color-scheme:dark}
    [data-hoymiles-update-dialog]::backdrop{background:#050c14bb}
    [data-hoymiles-update-dialog] *{box-sizing:border-box}
    [data-hoymiles-update-dialog] header{position:sticky;top:0;background:#101e28;display:flex;align-items:center;justify-content:space-between;padding:16px 24px;border-bottom:1px solid #365568;z-index:1}
    [data-hoymiles-update-dialog] h1{font-size:20px;margin:0}[data-hoymiles-update-dialog] h2{font-size:18px;color:#8cdffb;margin:0 0 10px}
    [data-hoymiles-update-dialog] main{padding:22px 24px}[data-hoymiles-update-dialog] section{padding:19px 0;border-bottom:1px solid #29414f}
    [data-hoymiles-update-dialog] p{margin:10px 0}[data-hoymiles-update-dialog] label{display:block;font-weight:650;margin:12px 0 7px}
    [data-hoymiles-update-dialog] textarea{width:100%;resize:vertical;min-height:100px;padding:12px;background:#0b141d;border:1px solid #486375;color:#e5f1f7;border-radius:8px;font:13px/1.5 monospace}
    [data-hoymiles-update-dialog] input{max-width:100%;margin:6px 0}[data-hoymiles-update-dialog] button{cursor:pointer;border:1px solid #4b6b80;border-radius:9px;background:#162c3a;color:#e5f1f7;font:inherit;padding:9px 14px;margin:4px 6px 4px 0}
    [data-hoymiles-update-dialog] button.primary{background:#0b8196;border-color:#45d8ed;font-weight:700}[data-hoymiles-update-dialog] button:disabled{opacity:.45;cursor:not-allowed}
    [data-hoymiles-update-dialog] a{color:#7fddff}[data-hoymiles-update-dialog] .note{background:#163240;border-left:3px solid #63c4e9;padding:13px 16px;border-radius:6px}
    [data-hoymiles-update-dialog] .warn{background:#392f1d;border-color:#e2ad44}[data-hoymiles-update-dialog] .error{color:#ffb6b6}
    [data-hoymiles-update-dialog] small{color:#b4c7d3}[data-hoymiles-update-dialog] .result:empty{display:none}[data-hoymiles-update-dialog] pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
    [data-hoymiles-update-dialog] :focus-visible{outline:3px solid #72dcff;outline-offset:3px}
    @media(max-width:500px){[data-hoymiles-update-dialog] main,[data-hoymiles-update-dialog] header{padding:15px}[data-hoymiles-update-dialog] h1{font-size:18px}}
    </style><header><h1>${t.title}</h1><button data-close aria-label="${t.close}">×</button></header><main>
    <p>${t.intro}</p><small>${t.steps}</small>
    <section><h2>${t.newsTitle}</h2><p>${t.news}</p><a href="https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/releases/tag/v1.5.8.1" target="_blank" rel="noopener noreferrer">${t.notes}</a><p class="note">${t.current}</p></section>
    <section><h2>${t.before}</h2><p>${t.beforeText}</p><p>${t.ha}</p></section>
    <section><h2>${t.yamlTitle}</h2><p>${t.yamlHelp}</p><label for="hm-update-file">${t.fileLabel}</label><input id="hm-update-file" type="file" accept=".yaml,.yml"><label for="hm-update-source">${t.sourceLabel}</label><textarea id="hm-update-source" rows="6" spellcheck="false" autocomplete="off"></textarea><p><small>${t.privacy}</small></p></section>
    <section><h2>${t.logTitle}</h2><p>${t.logHelp}</p><label for="hm-update-log">${t.logLabel}</label><textarea id="hm-update-log" rows="4" spellcheck="false" autocomplete="off"></textarea><p class="note" data-transport aria-live="polite">${t.unknown}</p><button class="primary" data-prepare disabled>${t.prepare}</button><p data-admin></p></section>
    <div class="result" data-result aria-live="polite"></div>
    <p><a href="https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/blob/v1.5.8.1/docs/UPGRADE_1_5_7.md" target="_blank" rel="noopener noreferrer">${t.support}</a></p>
    <button data-read>${t.read}</button></main>`;
  const $ = (selector) => dialog.querySelector(selector);
  const source = $("#hm-update-source"), log = $("#hm-update-log");
  const prepare = $("[data-prepare]"), result = $("[data-result]");
  let localPackages, busy = false, reading = false, generation = 0;
  const refreshButton = () => { prepare.disabled = busy || reading || !hass?.user?.is_admin || !source.value.trim() || classifyOtaLog(log.value) === "unknown"; };
  const add = (tag, text, parent = result) => { const element = document.createElement(tag); element.textContent = text; parent.append(element); return element; };
  const download = (name, contents) => {
    const url = URL.createObjectURL(new Blob([contents], {type:"text/yaml;charset=utf-8"}));
    const a = document.createElement("a"); a.href = url; a.download = name; dialog.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const update = () => {
    generation += 1; result.replaceChildren();
    const transport = classifyOtaLog(log.value);
    $("[data-transport]").textContent = t[transport];
    refreshButton();
  };
  source.addEventListener("input", () => { localPackages = undefined; update(); }); log.addEventListener("input", update);
  $("#hm-update-file").addEventListener("change", async (event) => {
    const file = event.target.files?.[0]; if (!file) return;
    source.value = ""; localPackages = undefined; update();
    if (file.size > 131072 || /^secrets\.ya?ml$/i.test(file.name)) { add("p", /^secrets\.ya?ml$/i.test(file.name) ? t.noSecrets : t.tooLarge).className = "error"; return; }
    const currentGeneration = generation;
    reading = true; refreshButton();
    try {
      const text = await file.text();
      if (dialog.isConnected && currentGeneration === generation) { source.value = text; update(); }
    } catch (_error) { if (dialog.isConnected && currentGeneration === generation) add("p", t.fileRead).className = "error"; }
    finally { reading = false; refreshButton(); }
  });
  if (!hass?.user?.is_admin) $("[data-admin]").textContent = t.admin;
  prepare.addEventListener("click", async () => {
    if (busy || prepare.disabled) return;
    const currentGeneration = ++generation;
    busy = true; prepare.disabled = true; prepare.textContent = t.busy; result.replaceChildren();
    try {
      const response = await hass.fetchWithAuth("/api/hoymiles_hit_modbus/prepare-esphome-upgrade", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body:JSON.stringify({source:source.value, transport:classifyOtaLog(log.value), local_packages:localPackages}),
      });
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? "admin" : "network");
      const data = await response.json();
      if (!dialog.isConnected || currentGeneration !== generation) return;
      if (data.status === "need_packages") {
        add("h2", t.localTitle); add("p", t.localText); add("pre", data.files.join("\n"));
        const label = add("label", t.packagesLabel); label.htmlFor = "hm-update-packages";
        const input = add("input", ""); input.id = label.htmlFor; input.type = "file"; input.multiple = true; input.accept = ".yaml,.yml";
        input.addEventListener("change", async () => {
          const currentGeneration = ++generation;
          const selected = Object.create(null);
          localPackages = undefined; reading = true; refreshButton();
          try {
            for (const file of input.files) {
              const key = `packages/${file.name}`;
              if (!data.files.includes(key) || selected[key] !== undefined || file.size > 1048576) throw new Error("incomplete_packages");
              selected[key] = await file.text();
            }
            if (Object.keys(selected).length !== data.files.length) throw new Error("incomplete_packages");
            if (dialog.isConnected && currentGeneration === generation) { localPackages = selected; prepare.textContent = t.retry; }
          } catch (_error) { if (dialog.isConnected && currentGeneration === generation) add("p", t.errors.incomplete_packages).className = "error"; }
          finally { reading = false; refreshButton(); }
        });
      } else if (data.status === "ready") {
        add("h2", t.ready); add("p", t.prepared);
        const backupButton = add("button", t.backup); backupButton.type = "button";
        const fileButton = add("button", t.download); fileButton.type = "button"; fileButton.disabled = true; fileButton.className = "primary";
        const backupNote = add("small", t.backupFirst);
        backupButton.addEventListener("click", () => { download("hoymiles-before-update.yaml", data.backup); fileButton.disabled = false; backupNote.hidden = true; });
        fileButton.addEventListener("click", () => download(`hoymiles-update-${data.stage}.yaml`, data.yaml));
        add("h2", t.install); add("p", t.installText);
        add("p", data.stage === "bridge" ? t.bridge : t.final).className = "note warn";
        const details = add("details", ""); add("summary", t.details, details);
        add("pre", `ESP: ${data.firmware_version}\nBoard: ${data.board}\nSHA: ${data.firmware_sha}\nYAML SHA256: ${data.sha256}`, details);
      } else { add("h2", t.failed); add("p", t.errors[data.code] || t.errors.unsupported_yaml).className = "error"; }
      result.scrollIntoView({block:"nearest"});
    } catch (error) { if (dialog.isConnected && currentGeneration === generation) add("p", error.message === "admin" ? t.admin : t.network).className = "error"; }
    finally { busy = false; prepare.textContent = t.prepare; refreshButton(); }
  });
  $("[data-close]").onclick = () => dialog.close();
  $("[data-read]").onclick = () => {
    try { localStorage.setItem("hoymiles-update-guide-1.5.8.1", "read"); } catch (_error) { /* The guide remains usable with storage disabled. */ }
    window.dispatchEvent(new Event("hoymiles-update-guide-read")); dialog.close();
  };
  dialog.addEventListener("close", () => { generation += 1; source.value = ""; log.value = ""; localPackages = undefined; dialog.remove(); }, {once:true});
  document.body.append(dialog); dialog.showModal(); $("[data-close]").focus();
  return dialog;
}
