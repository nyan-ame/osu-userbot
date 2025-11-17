import asyncio
import signal
import sys
import time # Для короткой паузы в поллинге JSON (если данные Tosu загружаются с задержкой)
from telethon import TelegramClient, events
import requests
import json
from datetime import timedelta

# Настройки Telegram (получите на my.telegram.org)
API_ID = 1
API_HASH = '2'
PHONE = '+3'
OSU_API_KEY = '4'  # Получи на osu.ppy.sh/p/api
OSU_USER_ID = 5  # Твой osu! user ID

# Порт gosumemory (по умолчанию 24050)
GOSU_PORT = 24050

# Per-user кулдаун в RAM
COOLDOWN = 45  # секунд, настрой под себя
last_response = {}  # {user_id: timestamp} — хранится в памяти
cooldown_lock = asyncio.Lock()  # Для thread-safety

client = TelegramClient(
    'osu_userbot_session',  # Имя сессии (файл .session сохраняется автоматически)
    API_ID,
    API_HASH,
    device_model='Samsung SM-G998B',
    system_version='11',
    app_version='10.8.0',
    lang_code='en',
    system_lang_code='en-US'
)

def get_mode_display(mode, cs=None):
    """
    Формирует отображаемое название режима на основе gameMode и CS (для mania).
    - mode 0: "std" (standard)
    - mode 1: "taiko"
    - mode 2: "catch"
    - mode 3: "mania X Key(s)" (X = CS из stats, 1 Key для 1, Keys для остальных)
    CS — количество колонок в mania (1–18).
    """
    if mode == 0:
        return "std"
    elif mode == 1:
        return "taiko"
    elif mode == 2:
        return "catch"
    elif mode == 3:
        if cs is None:
            cs = 4 # Минимальный fallback (стандарт 4K для mania)
        if cs == 1:
            return f"mania (1K)"
        else:
            return f"mania ({cs}K)"
    return "unknown" # Неизвестный режим (fallback для ошибок)

def get_osu_status():
    """
    Основная функция: Получает текущий статус osu! из Tosu/gosumemory.
    
    Логика:
    1. Проверяет, запущена ли игра (gameplay present + score > 0).
    2. Определяет режим по gameMode (0=std, 3=mania и т.д.).
    3. Захватывает fullSR (SR с модами) и mods (строка модов) с 1 попыткой поллинга.
    4. Fallback: Если fullSR=0, берёт base SR из osu! API (&m=mode).
    5. Для mania: CS из stats.CS для отображения "X Keys".
    6. Загружает метаданные карты из API (artist, title, version, length).
    7. Возвращает HTML-форматированную строку для Telegram.
    
    Returns:
        str or None: HTML-строка статуса или None, если не playing.
    """
    try:
        # Базовый запрос для проверки статуса игры
        response = requests.get(f'http://127.0.0.1:{GOSU_PORT}/json', timeout=5)
        if response.status_code != 200:
            print("Gosu/Tosu недоступен (код:", response.status_code, ")")
            return None
        data = response.json()
        
        # Debug: базовый статус
        state = data.get('menu', {}).get('state', -1)
        mode = data.get('menu', {}).get('gameMode', 0)  
        if mode == 0:
            mode = data.get('gameplay', {}).get('gameMode', 0)
        gameplay_present = 'gameplay' in data
        score_obj = data.get('gameplay', {}).get('score', {})
        if isinstance(score_obj, dict):
            if mode == 3:   # Mania использует total score (сумма нот)
                score_current = score_obj.get('total', 0)
            else:           # Standard/taiko/catch: current score
                score_current = score_obj.get('current', 0)
        else:
            score_current = score_obj
        print(f"Debug: Mode={mode}, State={state}, Gameplay present={gameplay_present}, Score current={score_current}")
        
        # Проверка active playing: gameplay присутствует и score >0
        if not gameplay_present or score_current == 0:
            return None
        
        beatmap_id = data.get('menu', {}).get('bm', {}).get('id', 0)
        if beatmap_id == 0 or not OSU_API_KEY:
            print("Нет beatmap_id или API key — молчу.")
            return None
        
        # Поллинг для fullSR и mods: 1 попытка (минимальный запрос для стабильности)
        full_sr = 0
        mods_str = ''
        cs = None  # Для mania: количество колонок из stats.CS
        has_full_sr = False
        has_mods = False
        for attempt in range(1):  # 1 попытка (расширьте до 3–5, если обнаруживаются какие-то проблемы с выводом информации в сообщениях)
            mod_response = requests.get(f'http://127.0.0.1:{GOSU_PORT}/json', timeout=2)
            if mod_response.status_code == 200:
                mod_data = mod_response.json()
                stats = mod_data.get('menu', {}).get('bm', {}).get('stats', {})
                current_full_sr = stats.get('fullSR', 0)
                print(f"Debug poll {attempt+1}: fullSR raw: {current_full_sr}")
                if current_full_sr > 0 and not has_full_sr:
                    full_sr = current_full_sr
                    has_full_sr = True
                    print(f"Debug: Full SR found on attempt {attempt+1}: {full_sr}")
                
                # Для mania: CS из stats.CS (количество колонок для "XK")
                if mode == 3:
                    cs = stats.get('CS', 4)
                    print(f"Debug poll {attempt+1}: Mania CS: {cs}")
                
                # Mods из leaderboard.ourplayer.mods (live во время игры)
                leaderboard_mods = mod_data.get('gameplay', {}).get('leaderboard', {}).get('ourplayer', {}).get('mods', '')
                print(f"Debug poll {attempt+1}: leaderboard_mods raw: '{leaderboard_mods}'")
                if leaderboard_mods and not has_mods:
                    mods_str = leaderboard_mods
                    has_mods = True
                    print(f"Debug: Mods str found on attempt {attempt+1} from leaderboard: '{mods_str}'")
                    break  # если моды не найдены (nomode)
                
                # Fallback mods: bm.mods или gameplay.mods (строка модов)
                bm_mods = mod_data.get('menu', {}).get('bm', {}).get('mods', {})
                gameplay_mods = mod_data.get('gameplay', {}).get('mods', {})
                print(f"Debug poll {attempt+1} fallback: bm_mods raw: {bm_mods}, gameplay_mods raw: {gameplay_mods}")
                fallback_mods = bm_mods.get('str', '') or gameplay_mods.get('str', '')
                if fallback_mods and not has_mods:
                    mods_str = fallback_mods
                    has_mods = True
                    print(f"Debug: Mods str found on attempt {attempt+1} from fallback: '{mods_str}'")
                    break  # если моды не найдены (nomode)
                
                # Парсинг bm.mods.num если str пуст (битфлаги для модов)
                if not fallback_mods and bm_mods.get('num', 0) > 0:
                    mods_num = bm_mods.get('num', 0)
                    mod_map = {2: 'EZ', 4: 'TD', 8: 'HD', 16: 'HR', 32: 'SD', 64: 'DT', 128: 'RL', 256: 'HT', 512: 'NC', 1024: 'FL', 2048: 'AP', 4096: 'SO', 16384: 'PF', 32768: '4K', 65536: '5K', 131072: '6K', 262144: '7K', 524288: '8K', 1048576: '9K'}
                    mod_list = [v for k, v in mod_map.items() if mods_num & k]
                    mods_str = ''.join(mod_list)
                    if mods_str:
                        has_mods = True
                        print(f"Debug: Mods str from num bitflags: '{mods_str}'")
                    break
            
            # Если fullSR и моды оба найдены — break
            if has_full_sr and has_mods:
                break
            time.sleep(0.2) # Короткая пауза между попытками (если расширите range)
        
        # Fallback на base SR из osu! API, если fullSR=0 (Tosu не загрузил)
        if full_sr == 0:
            bm_api_url = f'https://osu.ppy.sh/api/get_beatmaps?b={beatmap_id}&k={OSU_API_KEY}&m={mode}'
            bm_resp = requests.get(bm_api_url, timeout=5).json()
            if bm_resp:
                full_sr = float(bm_resp[0].get('difficultyrating', 0))
                print(f"Debug: Fallback base SR from API (mode {mode}): {full_sr}")
            else:
                # Auto-detect mode: Если &m=0 пустой, пробуем &m=3 (для mania-bugs в Tosu)
                if mode == 0:
                    bm_api_url_m3 = f'https://osu.ppy.sh/api/get_beatmaps?b={beatmap_id}&k={OSU_API_KEY}&m=3'
                    bm_resp_m3 = requests.get(bm_api_url_m3, timeout=5).json()
                    if bm_resp_m3:
                        mode = 3  # Auto-correct to mania
                        full_sr = float(bm_resp_m3[0].get('difficultyrating', 0))
                        print(f"Debug: Auto-corrected mode to 3 (mania), base SR: {full_sr}")
        
        print(f"Debug: Mods str final: '{mods_str}'")  # Финальный debug
        
        # Данные карты из osu! API (с mode-specific параметром &m={mode})
        bm_api_url = f'https://osu.ppy.sh/api/get_beatmaps?b={beatmap_id}&k={OSU_API_KEY}&m={mode}'
        bm_resp = requests.get(bm_api_url, timeout=5).json()
        if not bm_resp:
            return None # Нет данных карты — не можем сформировать ответ
        bm = bm_resp[0]
        artist = bm.get('artist', 'Unknown')
        title = bm.get('title', 'Unknown')
        difficulty = bm.get('version', 'Unknown')
        map_name = f"{artist} - {title} [{difficulty}]"
        hit_length_sec = int(bm.get('hit_length', 0))
        print(f"Debug: Map: {map_name}, Length sec: {hit_length_sec}")
        
        # Вывод режима (cs из Tosu для mania)
        mode_display = get_mode_display(mode, cs=cs)
        
        # HTML-ссылка
        map_link = f'<a href="https://osu.ppy.sh/b/{beatmap_id}">{map_name}</a>'
        
        # Форматированный SR (fullSR из Tosu с округлением)
        star_formatted = f"{round(full_sr, 2):.2f}⭐️"
        mods_formatted = f" +{mods_str}" if mods_str else ""
        star_line = f"{star_formatted}{mods_formatted}"
        
        # Длина
        length_min = hit_length_sec // 60
        length_sec = hit_length_sec % 60
        length = f"{length_min}:{length_sec:02d}"
        
        # HTML-структура ответа для Telegram (parse_mode='html')
        html_status = f"""
<b>now playing osu!{mode_display}</b><br>
Map: {map_link}<br>
Star: {star_line}<br>
Length: {length}
"""
        return html_status.strip()
    
    except Exception as e:
        print(f"Ошибка в get_osu_status: {e}")
        return None

@client.on(events.NewMessage(incoming=True))
async def message_handler(event):
    if not event.is_private:
        return
    
    print(f"Получено сообщение: chat_id={event.chat_id}, is_private={event.is_private}, text='{event.text[:50]}...'")  # Debug только для ЛС
    
    user_id = event.chat_id
    current_time = time.time()
    
    # Per-user кулдаун с lock'ом
    async with cooldown_lock:
        if user_id in last_response and current_time - last_response[user_id] < COOLDOWN:
            remaining = COOLDOWN - (current_time - last_response[user_id])
            print(f"Кулдаун для {user_id}: жду {remaining:.0f}с")
            return
    
    print("Получено ЛС — проверяю статус osu!...")
    status = get_osu_status()
    if status:
        await event.respond(status, parse_mode='html')
        print("Ответ отправлен!")
        # Обновляем кулдаун
        async with cooldown_lock:
            last_response[user_id] = current_time
    else:
        print("Статус не playing — молчу.")

async def main():
    await client.start(phone=PHONE)
    print("Userbot для telegram запущен!")
    print("При любом полученном личном сообщении во время игры автоматически оправляет информацию (карта, старрейт, моды, длина)")
    print("По умолчанию и всегда включен debug режим.")
    def signal_handler(sig, frame):
        print("\nОтключаюсь...")
        asyncio.create_task(client.disconnect())
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
