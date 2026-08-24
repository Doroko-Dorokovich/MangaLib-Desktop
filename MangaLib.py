import webview
import json
import os
import sys
import threading
import time
import asyncio
from pypresence import Presence
from pypresence.types import ActivityType
from pypresence.exceptions import DiscordNotFound, InvalidID

CLIENT_ID = "1513182971027263611"
discord_rpc = None
discord_stop = threading.Event()
current_site_for_rpc = "mangalib"
loop = None

# Детальное состояние чтения/просмотра, которое обновляется из JS (см. rpcTracker в on_loaded)
rpc_lock = threading.Lock()
rpc_state = {"mode": None, "updated_at": 0}

window = None
api = None

def run_async(coro):
    global loop
    if loop is None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return asyncio.run_coroutine_threadsafe(coro, loop)

def build_activity(site, state, session_start):
    """
    Собирает payload для discord_rpc.update() на основе детального состояния,
    которое прислал JS (rpcTracker) через pywebview.api.push_rpc_data().
    Если детальных данных нет (пользователь не на странице чтения/просмотра) —
    откатываемся на старое поведение "просто сидит на сайте".
    """
    mode = state.get("mode")

    if mode == "manga":
        title = state.get("title") or "Манга"
        chapter = state.get("chapter")
        volume = state.get("volume")
        page = state.get("page")
        pages_total = state.get("pages_total")

        parts = []
        if volume:
            parts.append(f"Том {volume}")
        if chapter:
            parts.append(f"Глава {chapter}")
        if page:
            parts.append(f"стр. {page}/{pages_total}" if pages_total else f"стр. {page}")

        return {
            "activity_type": ActivityType.WATCHING,
            "details": f"📖 {title}"[:128],
            "state": (", ".join(parts) if parts else "Читает")[:128],
            "large_image": "mangalib",
            "large_text": "MangaLib App",
        }

    if mode == "anime":
        title = state.get("title") or "Аниме"
        episode = state.get("episode")
        studio = state.get("studio")
        position = state.get("position")
        duration = state.get("duration")
        playing = state.get("playing", True)

        parts = []
        if episode:
            parts.append(f"Серия {episode}")
        if studio:
            parts.append(studio)

        payload = {
            "activity_type": ActivityType.WATCHING,
            "details": f"🎬 {title}"[:128],
            "state": (" · ".join(parts) if parts else "Смотрит")[:128],
            "large_image": "animelib",
            "large_text": "MangaLib App",
        }

        # start/end — Discord сам анимирует полосу прогресса на клиенте, доп. апдейты для этого не нужны.
        # Если видео на паузе — не передаём таймкоды, чтобы полоса не "тикала" вхолостую.
        if playing and position is not None and duration:
            now = time.time()
            payload["start"] = int(now - position)
            payload["end"] = int(now - position + duration)

        return payload

    # Нет данных о чтении/просмотре — просто "на сайте"
    if site == "mangalib":
        details, state_text, large_image = "📖 Читает мангу", "на MangaLib", "mangalib"
    else:
        details, state_text, large_image = "🎬 Смотрит аниме", "на AnimeLib", "animelib"

    return {
        "activity_type": ActivityType.WATCHING,
        "details": details,
        "state": state_text,
        "large_image": large_image,
        "large_text": "MangaLib App",
        "start": session_start,
    }


async def async_discord_worker():
    global discord_rpc, current_site_for_rpc
    await asyncio.sleep(1)
    for pipe in range(10):
        try:
            discord_rpc = Presence(CLIENT_ID, pipe=pipe)
            await asyncio.wait_for(asyncio.get_event_loop().run_in_executor(None, discord_rpc.connect), timeout=5)
            print(f"[Discord] Подключён через pipe={pipe}")
            break
        except Exception as e:
            print(f"[Discord] pipe={pipe} не работает: {e}")
            continue
    else:
        print("[Discord] Не удалось подключиться ни к одному pipe")
        return

    start_time = int(time.time())
    last_sent_key = None

    # Проверяем состояние раз в секунду, но шлём в Discord, только когда что-то
    # реально изменилось (смена главы/страницы/серии, старт/пауза видео и т.д.) —
    # это и даёт "мгновенное" обновление без спама запросами.
    while not discord_stop.is_set():
        try:
            with rpc_lock:
                state_snapshot = dict(rpc_state)

            payload = build_activity(current_site_for_rpc, state_snapshot, start_time)
            send_key = (
                payload.get("details"),
                payload.get("state"),
                payload.get("large_image"),
                payload.get("start"),
                payload.get("end"),
            )

            if send_key != last_sent_key:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda p=payload: discord_rpc.update(**p)
                )
                last_sent_key = send_key
        except Exception as e:
            print(f"[Discord] Ошибка обновления: {e}")

        await asyncio.sleep(1)

def discord_thread_func():
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(async_discord_worker())

def start_discord():
    t = threading.Thread(target=discord_thread_func, daemon=True)
    t.start()

def stop_discord():
    discord_stop.set()
    if discord_rpc:
        try:
            discord_rpc.close()
        except:
            pass

class Api:
    def __init__(self):
        self.current_site = self.load_site()
        self.current_url = self.get_site_url(self.current_site)
        self.update_rpc_site(self.current_site)

    def get_site_url(self, site):
        if site == 'mangalib':
            return 'https://mangalib.me/ru'
        else:
            return 'https://v5.animelib.org/ru'

    def load_site(self):
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))

        config_file = os.path.join(base_path, 'config.json')
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    config = json.load(f)
                    return config.get('site', 'mangalib')
            except:
                return 'mangalib'
        return 'mangalib'

    def save_site(self, site):
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))

        config_file = os.path.join(base_path, 'config.json')
        try:
            with open(config_file, 'w') as f:
                json.dump({'site': site}, f)
        except:
            pass

    def get_current_site(self):
        return self.current_site

    def set_site(self, site):
        self.current_site = site
        self.save_site(site)
        self.update_rpc_site(site)

    def update_rpc_site(self, site):
        global current_site_for_rpc
        current_site_for_rpc = site

    def toggle_fullscreen(self):
        window.toggle_fullscreen()

    def push_rpc_data(self, data):
        """Вызывается из инжектнутого JS (rpcTracker) при смене главы/страницы/серии/позиции видео."""
        global rpc_state
        with rpc_lock:
            if not data or data.get("mode") is None:
                rpc_state = {"mode": None, "updated_at": time.time()}
            else:
                data["updated_at"] = time.time()
                rpc_state = data

def on_loaded():
    global current_site_for_rpc
    current_site = api.get_current_site()
    js_code = f"""
        let lang = 'ru';
        let currentSite = '{current_site}';

        const texts = {{
            ru: {{ full: 'Полноэкранный', window: 'Оконный (с рамками)', switch: 'Переключить сайт', cancel: 'Отмена' }},
            en: {{ full: 'Fullscreen', window: 'Windowed (with borders)', switch: 'Switch site', cancel: 'Cancel' }}
        }};
        let isFullscreen = false;

        function updateTexts() {{
            const t = texts[lang];
            const fullBtn = document.getElementById('fullscreenBtn');
            if (fullBtn) fullBtn.innerText = isFullscreen ? t.window : t.full;
            const switchBtn = document.getElementById('switchBtn');
            if (switchBtn) {{
                const target = currentSite === 'mangalib' ? 'AnimeLib' : 'MangaLib';
                switchBtn.innerText = t.switch + ' (' + target + ')';
            }}
            const cancelBtn = document.getElementById('cancelBtn');
            if (cancelBtn) cancelBtn.innerText = t.cancel;
        }}

        function showMenu() {{
            if (document.getElementById('customMenu')) return;
            let menu = document.createElement('div');
            menu.id = 'customMenu';
            menu.style.cssText = 'position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%); background: #2d2d2d; color: white; border-radius: 8px; padding: 20px; z-index: 10000; font-family: sans-serif; text-align: center; box-shadow: 0 0 15px black; min-width: 220px;';
            menu.innerHTML = `
                <div><button id="langRu">RU</button> <button id="langEn">EN</button></div>
                <button id="fullscreenBtn" style="margin-top:10px; width:100%"></button>
                <button id="switchBtn" style="margin-top:10px; width:100%"></button>
                <button id="cancelBtn" style="margin-top:10px; width:100%"></button>
            `;
            document.body.appendChild(menu);

            document.getElementById('langRu').onclick = () => {{ lang = 'ru'; updateTexts(); }};
            document.getElementById('langEn').onclick = () => {{ lang = 'en'; updateTexts(); }};
            document.getElementById('fullscreenBtn').onclick = () => {{
                pywebview.api.toggle_fullscreen();
                isFullscreen = !isFullscreen;
                updateTexts();
            }};
            document.getElementById('switchBtn').onclick = () => {{
                const newSite = currentSite === 'mangalib' ? 'animelib' : 'mangalib';
                pywebview.api.set_site(newSite);
                const newUrl = newSite === 'mangalib' ? 'https://mangalib.me/ru' : 'https://v5.animelib.org/ru';
                window.location.href = newUrl;
                menu.remove();
            }};
            document.getElementById('cancelBtn').onclick = () => menu.remove();
            updateTexts();
        }}

        document.addEventListener('keydown', function(e) {{
            if (e.key === 'F11') {{
                e.preventDefault();
                showMenu();
            }}
        }});

        (function adBlocker() {{
            // 0. CSS-правило подключаем СРАЗУ, до всего остального: оно скрывает картинки
            // промо-баннера моментально и не зависит от того, успеет ли JS-логика ниже
            // найти и удалить нужный контейнер — работает даже на баннерах, которые
            // подгрузятся уже после первого прохода MutationObserver.
            if (!document.getElementById('mangalib-desktop-hide-style')) {{
                const style = document.createElement('style');
                style.id = 'mangalib-desktop-hide-style';
                style.textContent = 'img[src*="/uploads/slider_items/"] {{ display: none !important; }}';
                document.head.appendChild(style);
            }}

            // 1. Уничтожаем известные контейнеры сразу
            function killContainers() {{
                // Раньше здесь был список хэш-классов конкретной рекламы (o6_b, o6_ce и т.п.) —
                // такие классы Vue пересобирает заново при каждом деплое сайта, и один раз
                // они случайно совпали с обёртками картинок в карточках манги/аниме, из-за
                // чего пропадали превью. Оставляем только текстовый поиск — он безопасен,
                // т.к. завязан на видимую надпись, а не на генерируемые классы.
                const buttons = document.querySelectorAll('button, a');
                buttons.forEach(btn => {{
                    if (btn.innerText && btn.innerText.includes('Отключить рекламу')) {{
                        let parent = btn.parentElement;
                        if (parent && parent.children.length <= 3) parent.remove();
                        else btn.remove();
                    }}
                }});
            }}

            // 2. Уничтожаем iframe с подозрительными источниками
            function killIframes() {{
                const iframes = document.querySelectorAll('iframe');
                iframes.forEach(iframe => {{
                    const src = iframe.src || '';
                    if (src.includes('doubleclick') || src.includes('googlead') || 
                        src.includes('yastatic') || src.includes('adriver') ||
                        src.includes('googlesyndication') || src.includes('exmarket') ||
                        src.includes('popunder')) {{
                        iframe.remove();
                    }}
                }});
            }}

            // 3. Уничтожаем карточки с конкретными словами
            function killBadCards() {{
                const cards = document.querySelectorAll('div[class*="card"], div[class*="item"], div[class*="offer"]');
                cards.forEach(card => {{
                    const text = card.innerText || '';
                    if (text.includes('Телеграм') || text.includes('узел бесплатно') ||
                        text.includes('Единоразовая выплата') || text.includes('COUPLEPLAMA') ||
                        text.includes('Квартиры в Ростове') || text.includes('Нейросети защитят') ||
                        (text.includes('Скачать') && text.includes('yandex'))) {{
                        card.remove();
                    }}
                }});
            }}

            // 3.1. Полноэкранная реклама (баннер "РЕКЛАМА • 16+" при смене главы).
            // ID/классы у неё рандомные на каждую загрузку, но виджет Яндекса всегда
            // помечает свои узлы стабильным атрибутом data-fullscreen-element-name
            // (подтверждено по присланной разметке) — по нему и ищем, поднимаясь до
            // корня оверлея, который лежит прямо в document.body.
            function killFullscreenAd() {{
                const marker = document.querySelector('[data-fullscreen-element-name]');
                if (!marker) return;
                let el = marker;
                while (el.parentElement && el.parentElement !== document.body) {{
                    el = el.parentElement;
                }}
                if (el && el.parentElement === document.body) {{
                    el.remove();
                }}
            }}

            // 3.2. Промо-слайдер MangaLib/AnimeLib (баннеры вида "Дарим подарок за активность",
            // "Магазин цифровых товаров" и т.п.). Путь /uploads/slider_items/ в src картинки
            // используется только у самих промо-баннеров (у обложек манги/аниме путь другой),
            // так что это надёжнее, чем цепляться за класс img, который может переиспользоваться.
            function isReasonableBannerSize(el) {{
                const r = el.getBoundingClientRect();
                // баннер-слайдер — невысокая полоса; если контейнер размером с половину окна
                // и больше, это уже не слайдер, а кусок страницы — не трогаем. Нулевую высоту
                // (картинка ещё не отрисовалась) не отсеиваем — это не признак большого блока.
                return r.height < Math.max(400, window.innerHeight * 0.4);
            }}

            function killPromoSlider() {{
                document.querySelectorAll('img[src*="/uploads/slider_items/"]').forEach(img => {{
                    // Поднимаемся вверх, пока размер остаётся разумным для баннера, и берём
                    // САМОГО ДАЛЬНЕГО такого предка — это и есть весь блок слайдера целиком
                    // (картинка + стрелки + точки-пагинация), а не просто сама картинка.
                    let target = img;
                    let el = img;
                    for (let i = 0; i < 6 && el.parentElement; i++) {{
                        el = el.parentElement;
                        if (isReasonableBannerSize(el)) {{
                            target = el;
                        }} else {{
                            break;
                        }}
                    }}
                    target.remove();
                }});
            }}

            // 3.3. Приписка "MangaLib Desktop" в подвале рядом со строкой юр. адреса
            // (ООО «Мангалиб»/«Анилиб») — именно её просили как якорь, а не строку
            // с почтой поддержки, поэтому строка с адресом ищется в первую очередь.
            // Кавычки вокруг названия на сайте могут быть любые (типографские и т.п.),
            // поэтому не завязываемся на конкретный символ кавычки.
            function injectCredit() {{
                let anchor = Array.from(document.querySelectorAll('div, span')).find(el =>
                    el.children.length === 0 && /ООО[^а-яА-Я]{{0,5}}(Мангалиб|Анилиб)/i.test(el.textContent || '')
                );
                if (anchor) anchor = anchor.parentElement;

                if (!anchor) {{
                    // Резерв, если строка с адресом не нашлась вовсе
                    const mailLink = document.querySelector(
                        'a[href^="mailto:info@mangalib.me"], a[href^="mailto:info@anilib.me"]'
                    );
                    anchor = mailLink ? mailLink.closest('div') : null;
                }}
                if (!anchor) return;

                let credit = document.getElementById('mangalib-desktop-credit');
                if (credit) {{
                    if (credit.parentElement === anchor) return; // уже на месте
                    credit.remove(); // висит не там (например, со старой версии скрипта) — переносим
                }} else {{
                    credit = document.createElement('span');
                    credit.id = 'mangalib-desktop-credit';
                    credit.style.cssText = 'margin-left:8px; opacity:0.7;';
                    credit.innerHTML = 'MangaLib Desktop, Doroko-Dorokovich, 2026. ' +
                        '<a href="https://github.com/Doroko-Dorokovich/MangaLib-Desktop" target="_blank" ' +
                        'style="color:inherit; text-decoration:underline;">GitHub</a>';
                }}
                anchor.appendChild(credit);
            }}

            // 4. Блокируем сетевые запросы к рекламным доменам (перехват fetch и XMLHttpRequest)
            const originalFetch = window.fetch;
            window.fetch = function(...args) {{
                const url = args[0];
                if (typeof url === 'string' && (
                    url.includes('doubleclick.net') || url.includes('googlead') ||
                    url.includes('yastatic.net') || url.includes('adriver.ru') ||
                    url.includes('googlesyndication') || url.includes('ads.') ||
                    url.includes('exmarket') || url.includes('popunder'))) {{
                    console.log('[Блокировщик] Заблокирован fetch:', url);
                    return new Promise(() => {{}});
                }}
                return originalFetch.apply(this, args);
            }};

            const originalXHROpen = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url, ...rest) {{
                if (typeof url === 'string' && (
                    url.includes('doubleclick.net') || url.includes('googlead') ||
                    url.includes('yastatic.net') || url.includes('adriver.ru') ||
                    url.includes('googlesyndication') || url.includes('ads.') ||
                    url.includes('exmarket') || url.includes('popunder'))) {{
                    console.log('[Блокировщик] Заблокирован XHR:', url);
                    return;
                }}
                return originalXHROpen.apply(this, [method, url, ...rest]);
            }};

            // 5. MutationObserver на страже
            const observer = new MutationObserver(() => {{
                killContainers();
                killIframes();
                killBadCards();
                killFullscreenAd();
                killPromoSlider();
                injectCredit();
            }});
            if (document.body) {{
                observer.observe(document.body, {{ childList: true, subtree: true }});
            }}

            killContainers();
            killIframes();
            killBadCards();
            killFullscreenAd();
            killPromoSlider();
            injectCredit();
        }})();

        // === Detailed Discord RPC tracker ===
        // Опрашивает страницу раз в секунду: если это читалка манги (URL вида /read/vX/cY) —
        // достаёт том/главу/страницу; если на странице есть <video> с известной длительностью —
        // считаем это просмотром аниме и достаём тайтл/серию/текущую позицию.
        // ВНИМАНИЕ: селекторы ниже — best-effort, подобраны без прямого доступа к живой разметке
        // сайта (бот-защита блокирует автоматический просмотр страниц). Если что-то не подхватится —
        // нужно посмотреть в DevTools (F12 в окне приложения при debug=True) реальные классы/структуру
        // и прислать их мне, чтобы поправить селекторы точнее.
        (function rpcTracker() {{
            let lastSent = null;

            function clean(str) {{
                return str ? str.replace(/\\s+/g, ' ').trim() : null;
            }}

            function textFromSelectors(selectors) {{
                for (const sel of selectors) {{
                    try {{
                        const el = document.querySelector(sel);
                        if (el && el.textContent && el.textContent.trim()) {{
                            return clean(el.textContent);
                        }}
                    }} catch (e) {{}}
                }}
                return null;
            }}

            function fallbackTitle() {{
                return clean(document.title.split(' — ')[0].split(' - ')[0].split('|')[0]);
            }}

            // Достаём общее название сайта из <title>/og:title вида
            // "Манга · Необъятный океан · Читать 1 главу" — проверено на реальной странице.
            function mangaTitleFromMeta() {{
                const og = document.querySelector('meta[property="og:title"]');
                const raw = (og && og.content) || document.title;
                // Формат вида "Манга · Название · Читать 1 главу" / "Манхва · Название · Читать 1 главу" —
                // тип контента перед первой точкой может быть разным (манга/манхва/маньхуа),
                // поэтому не привязываемся к конкретному слову.
                const m = raw.match(/^[^·]+·\\s*(.+?)\\s*·\\s*Читать/i);
                return m ? clean(m[1]) : null;
            }}

            function getMangaTitle() {{
                return mangaTitleFromMeta() || textFromSelectors(['h1', '.manga-title']) || fallbackTitle();
            }}

            // На AnimeLib название тайтла лежит в h1 > a на карточке (проверено на реальной странице).
            function getAnimeTitle() {{
                return textFromSelectors(['h1 a', 'h1', '.anime-title']) || fallbackTitle();
            }}

            function detectManga() {{
                const m = location.pathname.match(/\\/read\\/v(\\d+)\\/c([\\d.]+)/);
                if (!m) return null;

                const title = getMangaTitle();
                let page = null, pagesTotal = null;

                // Счётчик страниц в читалке — выпадающий список в футере,
                // текущее значение = select.value, всего страниц = число option'ов.
                const select = document.querySelector('footer select.form-input__field') || document.querySelector('footer select');
                if (select && select.value) {{
                    page = select.value;
                    if (select.options) pagesTotal = select.options.length;
                }}
                if (!page) {{
                    const pageParam = location.search.match(/[?&]page=(\\d+)/);
                    if (pageParam) page = pageParam[1];
                }}

                return {{
                    mode: 'manga',
                    title: title,
                    volume: m[1],
                    chapter: m[2],
                    page: page,
                    pages_total: pagesTotal
                }};
            }}

            // Текущая серия помечена доп. классом на элементе списка (id="episode_...").
            // У неактивных серий один класс, у активной — два (проверено на реальной странице).
            function getActiveEpisodeText() {{
                const items = document.querySelectorAll('[id^="episode_"]');
                for (const el of items) {{
                    const classes = (el.className || '').trim().split(/\\s+/);
                    if (classes.length > 1) {{
                        const span = el.querySelector('span');
                        if (span) return clean(span.textContent);
                    }}
                }}
                return null;
            }}

            // Активная озвучка/студия — пункт с классом is-active в списке "Озвучка".
            function getActiveStudio() {{
                return textFromSelectors(['.menu-item.is-active .menu-item__text']);
            }}

            function detectAnime() {{
                // Плеер аниме почти всегда встроен через <iframe class="st_pl"> со стороннего
                // домена (kodikplayer.com и т.п.) — это подтверждено на реальной странице.
                // Так и определяем режим просмотра, а не по наличию <video>, до которого
                // из-за кросс-доменной политики браузера напрямую не добраться.
                const playerFrame = document.querySelector('iframe.st_pl');
                if (!playerFrame) return null;

                const title = getAnimeTitle();
                const epText = getActiveEpisodeText();
                let episode = null;
                if (epText) {{
                    const em = epText.match(/(\\d+)/);
                    if (em) episode = em[1];
                }}
                const studio = getActiveStudio();

                let position = animeMediaState.position;
                let duration = animeMediaState.duration;
                let playing = animeMediaState.playing;

                // На случай, если у конкретного тайтла плеер оказался того же домена
                // (встречается у части аниме на самом AnimeLib) — тогда видео доступно напрямую
                // и даёт самые точные данные, без опоры на postMessage.
                try {{
                    const vid = playerFrame.contentDocument && playerFrame.contentDocument.querySelector('video');
                    if (vid && vid.duration && !isNaN(vid.duration)) {{
                        position = Math.floor(vid.currentTime);
                        duration = Math.floor(vid.duration);
                        playing = !vid.paused && !vid.ended;
                    }}
                }} catch (e) {{
                    // cross-origin — ожидаемо для стороннего плеера, используем данные из postMessage
                }}

                return {{
                    mode: 'anime',
                    title: title,
                    episode: episode,
                    studio: studio,
                    position: position,
                    duration: duration,
                    playing: playing !== false
                }};
            }}

            // === Позиция видео из плеера Kodik ===
            // <video> внутри чужого iframe недоступен напрямую (cross-origin), но плеер шлёт
            // состояние через postMessage — единственный легальный канал между доменами.
            // Формат подтверждён по реальному выводу консоли во время просмотра:
            //   {{key: 'kodik_player_time_update', value: <секунды>}}      — текущая позиция
            //   {{key: 'kodik_player_duration_update', value: <секунды>}}  — длительность
            //   {{key: 'kodik_player_play'}} / {{key: 'kodik_player_pause'}} — состояние
            // Другие сообщения от плеера (клики/движения мыши/аналитика) сюда не подмешиваем —
            // у них своё поле "time", но это unix-время в миллисекундах, а не позиция видео.
            const animeMediaState = {{ position: null, duration: null, playing: null }};

            window.addEventListener('message', function(ev) {{
                let data = ev.data;
                if (typeof data === 'string') {{
                    try {{ data = JSON.parse(data); }} catch (e) {{ return; }}
                }}
                if (!data || typeof data !== 'object' || !data.key) return;

                let changed = false;

                if (data.key === 'kodik_player_time_update' && typeof data.value === 'number') {{
                    const t = Math.floor(data.value);
                    if (animeMediaState.position !== t) {{ animeMediaState.position = t; changed = true; }}
                    if (animeMediaState.playing !== true) {{ animeMediaState.playing = true; changed = true; }}
                }} else if (data.key === 'kodik_player_duration_update' && typeof data.value === 'number') {{
                    const d = Math.floor(data.value);
                    if (animeMediaState.duration !== d) {{ animeMediaState.duration = d; changed = true; }}
                }} else if (data.key === 'kodik_player_play') {{
                    if (animeMediaState.playing !== true) {{ animeMediaState.playing = true; changed = true; }}
                }} else if (data.key === 'kodik_player_pause') {{
                    if (animeMediaState.playing !== false) {{ animeMediaState.playing = false; changed = true; }}
                }}

                if (changed) tick();
            }});

            function send(data) {{
                const key = JSON.stringify(data);
                if (key === lastSent) return;
                lastSent = key;
                if (window.pywebview && pywebview.api && pywebview.api.push_rpc_data) {{
                    pywebview.api.push_rpc_data(data);
                }}
            }}

            function tick() {{
                const manga = detectManga();
                if (manga) {{ send(manga); return; }}

                const anime = detectAnime();
                if (anime) {{ send(anime); return; }}

                send({{ mode: null }});
            }}

            setInterval(tick, 1000);
            tick();
        }})();
    """
    window.evaluate_js(js_code)

def main():
    global window, api
    start_discord()
    api = Api()
    window = webview.create_window(
        title='MangaLib',
        url=api.current_url,
        fullscreen=False,
        maximized=True,
        resizable=True,
        min_size=(800, 600),
        js_api=api
    )
    window.events.loaded += on_loaded
    webview.start(debug=False, http_server=True, private_mode=False)
    stop_discord()

if __name__ == '__main__':
    main()