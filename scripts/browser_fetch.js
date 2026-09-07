// Обход выдачи RuTracker силами самого браузера.
//
// Зачем: Cloudflare привязывает cf_clearance к отпечатку клиента, прошедшего
// проверку. Cookie, выданная Firefox (TLS-стек NSS), из httpx (OpenSSL) уже не
// принимается — это штатное поведение, а не сбой. Единственная конструкция,
// которая не разваливается при закручивании гаек, — та, где запросы физически
// исходят из браузера: тогда отпечаток совпадает по определению.
//
// Что делает: обходит все страницы выдачи и скачивает все .torrent, ничего не
// фильтруя. Фильтрация, дедупликация и именование остаются в CLI, где они уже
// написаны и покрыты тестами; дублировать их в JS нельзя — разойдутся.
// Скачивать всё подряд не жалко: .torrent весит 2-30 КБ.
//
// Как пользоваться:
//   1. В Firefox выключить HTTP/3 (about:config -> network.http.http3.enable
//      -> false), иначе тело ответа обрывается и файлы приходят нулевыми.
//   2. Открыть выдачу: https://rutracker.net/forum/tracker.php?nm=<запрос>
//   3. F12 -> Консоль -> вставить этот файл целиком -> Enter.
//   4. Разрешить браузеру скачивание нескольких файлов.
//
// Результат в каталоге загрузок:
//   rutracker-page-01.html ...  страницы выдачи (сырые байты, cp1251)
//   <topic_id>.torrent          сами торренты

(async () => {
  const DELAY_MS = 1000; // пауза между запросами: форум не должен страдать
  const MAX_PAGES = 50; // предохранитель, как --max-pages в CLI

  if (!location.pathname.includes("/forum/tracker.php")) {
    console.error("Открой страницу выдачи tracker.php и запусти снова.");
    return;
  }

  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

  // response.text() всегда декодирует как UTF-8, а форум отдаёт cp1251.
  // Поэтому берём сырые байты: их же и сохраняем, их же и декодируем вручную.
  const fetchBytes = async (url) => {
    const response = await fetch(url, { credentials: "include" });
    if (!response.ok) {
      throw new Error(`${url} -> HTTP ${response.status}`);
    }
    return new Uint8Array(await response.arrayBuffer());
  };

  const save = (bytes, filename, type) => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([bytes], { type }));
    link.download = filename;
    link.click();
    const objectUrl = link.href;
    setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  };

  const decoder = new TextDecoder("windows-1251");
  const parser = new DOMParser();

  const visited = new Set([location.href]);
  const queue = [location.href];
  const topicIds = new Set();
  let pageNumber = 0;

  while (queue.length > 0 && pageNumber < MAX_PAGES) {
    const url = queue.shift();
    let bytes;
    try {
      bytes = await fetchBytes(url);
    } catch (error) {
      console.error("страница пропущена:", error.message);
      continue;
    }

    pageNumber += 1;
    const label = String(pageNumber).padStart(2, "0");
    save(bytes, `rutracker-page-${label}.html`, "text/html");

    const tree = parser.parseFromString(decoder.decode(bytes), "text/html");

    for (const anchor of tree.querySelectorAll('a[href*="dl.php?t="]')) {
      const found = anchor.getAttribute("href").match(/[?&]t=(\d+)/);
      if (found) {
        topicIds.add(found[1]);
      }
    }

    // Ссылки пагинации берём из HTML, а не конструируем: RuTracker
    // подмешивает search_id, и собранный вручную URL вернёт не ту выдачу.
    for (const anchor of tree.querySelectorAll('a[href*="tracker.php"]')) {
      const href = anchor.getAttribute("href");
      if (!href || !href.includes("start=")) {
        continue;
      }
      const absolute = new URL(href, url).href;
      if (!visited.has(absolute) && absolute.startsWith(location.origin)) {
        visited.add(absolute);
        queue.push(absolute);
      }
    }

    console.log(`страница ${pageNumber}: раздач всего ${topicIds.size}`);
    await sleep(DELAY_MS);
  }

  if (queue.length > 0) {
    console.warn(`достигнут предел ${MAX_PAGES} страниц, осталось ${queue.length}`);
  }

  console.log(`обход завершён: страниц ${pageNumber}, раздач ${topicIds.size}`);

  let saved = 0;
  let failed = 0;
  for (const topicId of topicIds) {
    try {
      // Имя задаём сами: сервер отдаёт длинное имя с названием раздачи,
      // которое браузер обрезает по пределу файловой системы. CLI всё равно
      // берёт topic_id из содержимого файла, но с коротким именем в каталоге
      // загрузок можно разобраться глазами.
      save(await fetchBytes(`dl.php?t=${topicId}`), `${topicId}.torrent`, "application/x-bittorrent");
      saved += 1;
    } catch (error) {
      failed += 1;
      console.error(`${topicId}: ${error.message}`);
    }
    if ((saved + failed) % 10 === 0) {
      console.log(`скачано ${saved + failed} из ${topicIds.size}`);
    }
    await sleep(DELAY_MS);
  }

  console.log(`готово: скачано ${saved}, ошибок ${failed}`);
  console.log("дальше: uv run python -m rutracker_downloader --from-html ~/Загрузки/rutracker-page-*.html --import-downloads ~/Загрузки --output ./torrents/<имя>");
})();
