import webview

window = None

class Api:
    def toggle_fullscreen(self):
        window.toggle_fullscreen()

def main():
    global window
    window = webview.create_window(
        title='MangaLib',
        url='https://mangalib.me',
        fullscreen=False,
        maximized=True,
        resizable=True,
        min_size=(800, 600),
        js_api=Api()
    )

    def on_loaded():
        window.evaluate_js("""
            let lang = 'ru';
            let isFullscreen = false;
            const texts = {
                ru: { toggle_full: 'Полноэкранный', toggle_win: 'Оконный (с рамками)', cancel: 'Отмена' },
                en: { toggle_full: 'Fullscreen', toggle_win: 'Windowed (with borders)', cancel: 'Cancel' }
            };

            function updateToggleButton() {
                const btn = document.getElementById('toggleBtn');
                if (!btn) return;
                const t = texts[lang];
                btn.innerText = isFullscreen ? t.toggle_win : t.toggle_full;
            }

            async function showMenu() {
                if (document.getElementById('modeMenu')) return;
                let menu = document.createElement('div');
                menu.id = 'modeMenu';
                menu.style.cssText = `
                    position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
                    background: #1e1e1e; color: white; border: 1px solid #555; border-radius: 8px;
                    padding: 20px; z-index: 10000; font-family: sans-serif;
                    box-shadow: 0 0 15px rgba(0,0,0,0.5); min-width: 220px; text-align: center;
                `;
                menu.innerHTML = `
                    <div style="margin-bottom:10px">
                        <button id="langRu">RU</button>
                        <button id="langEn">EN</button>
                    </div>
                    <button id="toggleBtn" style="width:100%; margin-bottom:10px"></button><br>
                    <button id="cancelBtn" style="width:100%">Отмена</button>
                `;
                document.body.appendChild(menu);

                document.getElementById('langRu').onclick = () => { lang = 'ru'; updateToggleButton(); };
                document.getElementById('langEn').onclick = () => { lang = 'en'; updateToggleButton(); };
                document.getElementById('toggleBtn').onclick = async () => {
                    await pywebview.api.toggle_fullscreen();
                    isFullscreen = !isFullscreen;
                    menu.remove();
                };
                document.getElementById('cancelBtn').onclick = () => menu.remove();
                updateToggleButton();
            }

            document.addEventListener('keydown', function(e) {
                if (e.key === 'F11') {
                    e.preventDefault();
                    showMenu();
                }
            });
        """)
    window.events.loaded += on_loaded

    webview.start(debug=False, http_server=True, private_mode=False)

if __name__ == '__main__':
    main()