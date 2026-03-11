import json
import logging
import random
import threading
import time
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.http import JsonResponse
from django.shortcuts import render

LOTTO_API_BASE = "https://www.dhlottery.co.kr/common.do"
LOTTO_BACKUP_ALL_URL = "https://smok95.github.io/lotto/results/all.json"
LOTTO_MIN_NUMBER = 1
LOTTO_MAX_NUMBER = 45
LOTTO_DRAW_CACHE = {}
LOTTO_MISS_CACHE = set()
LOTTO_HTTP_TIMEOUT = 10
LOTTO_RETRY_COUNT = 3
LOTTO_WARMUP_LOCK = threading.Lock()
LOTTO_WARMUP_RUNNING = False
logger = logging.getLogger(__name__)


def home(request):
    return render(request, "portfolio/index.html")


def flyio_deploy(request):
    return render(request, "portfolio/flyio_deploy.html")


def trans_converter(request):
    return render(request, "portfolio/trans_converter.html")


def lotto_predictor(request):
    return render(request, "portfolio/lotto_predictor.html")


def _parse_lotto_json(raw_text):
    # 일부 환경에서 응답 앞뒤로 불필요한 텍스트가 섞여 들어올 수 있어 JSON 블록만 추출한다.
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        start = raw_text.find("{")
        end = raw_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            return json.loads(raw_text[start : end + 1])
        except json.JSONDecodeError:
            return None


def _load_lotto_draws_from_backup():
    try:
        request = Request(
            LOTTO_BACKUP_ALL_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PortfolioLottoBot/1.0)"},
        )
        with urlopen(request, timeout=LOTTO_HTTP_TIMEOUT) as response:
            payload = response.read().decode("utf-8-sig", errors="replace")
        data = json.loads(payload)
    except Exception as error:  # noqa: BLE001
        logger.warning("Failed to load lotto backup source: %s", error)
        return 0

    if not isinstance(data, list):
        logger.warning("Backup lotto source has unexpected format")
        return 0

    loaded = 0
    for item in data:
        draw_no = item.get("draw_no")
        numbers = item.get("numbers")
        bonus = item.get("bonus_no")
        draw_date = item.get("date", "")

        if not isinstance(draw_no, int):
            continue
        if draw_no in LOTTO_DRAW_CACHE:
            continue
        if not isinstance(numbers, list) or len(numbers) != 6:
            continue
        if not all(isinstance(num, int) for num in numbers):
            continue
        if not isinstance(bonus, int):
            continue

        LOTTO_DRAW_CACHE[draw_no] = {
            "draw_no": draw_no,
            "numbers": sorted(numbers),
            "bonus": bonus,
            "draw_date": str(draw_date)[:10],
        }
        loaded += 1

    return loaded


def _fetch_lotto_draw(draw_no):
    if draw_no in LOTTO_DRAW_CACHE:
        return LOTTO_DRAW_CACHE[draw_no]

    query = urlencode({"method": "getLottoNumber", "drwNo": draw_no})
    api_url = f"{LOTTO_API_BASE}?{query}"

    data = None
    for attempt in range(LOTTO_RETRY_COUNT):
        try:
            request = Request(
                api_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; PortfolioLottoBot/1.0)",
                    "Accept": "application/json, text/plain, */*",
                },
            )
            with urlopen(request, timeout=LOTTO_HTTP_TIMEOUT) as response:
                raw_text = response.read().decode("utf-8-sig", errors="replace")
                data = _parse_lotto_json(raw_text)
                if data is None:
                    logger.warning(
                        "Invalid lotto response format for draw %s (sample=%r)",
                        draw_no,
                        raw_text[:180],
                    )
                    raise json.JSONDecodeError("Invalid lotto payload", raw_text, 0)
            break
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            if attempt == LOTTO_RETRY_COUNT - 1:
                logger.warning("Failed to fetch lotto draw %s: %s", draw_no, error)
            else:
                time.sleep(0.35 * (attempt + 1))
        except Exception as error:  # noqa: BLE001
            logger.exception("Unexpected error while fetching lotto draw %s: %s", draw_no, error)
            break

    if not data:
        LOTTO_MISS_CACHE.add(draw_no)
        return None

    if data.get("returnValue") != "success":
        LOTTO_MISS_CACHE.add(draw_no)
        return None

    numbers = [data.get(f"drwtNo{i}") for i in range(1, 7)]
    bonus = data.get("bnusNo")
    if not all(isinstance(num, int) for num in numbers) or not isinstance(bonus, int):
        LOTTO_MISS_CACHE.add(draw_no)
        return None

    draw = {
        "draw_no": draw_no,
        "numbers": numbers,
        "bonus": bonus,
        "draw_date": data.get("drwNoDate", ""),
    }
    LOTTO_DRAW_CACHE[draw_no] = draw
    LOTTO_MISS_CACHE.discard(draw_no)
    return draw


def _find_latest_draw_no():
    # 3000회차는 충분히 큰 상한선으로 사용한다.
    low, high = 1, 3000
    if _fetch_lotto_draw(low) is None:
        logger.error("Unable to read lotto API baseline draw (draw_no=1)")
        return None

    while low < high:
        mid = (low + high + 1) // 2
        if _fetch_lotto_draw(mid):
            low = mid
        else:
            high = mid - 1
    return low


def _build_lotto_prediction(draws, set_count):
    scores = {number: 1.0 for number in range(LOTTO_MIN_NUMBER, LOTTO_MAX_NUMBER + 1)}
    frequency = {number: 0 for number in range(LOTTO_MIN_NUMBER, LOTTO_MAX_NUMBER + 1)}
    total_draws = len(draws)

    for index, draw in enumerate(draws):
        recent_weight = 0.45 + ((index + 1) / total_draws) * 1.55
        for number in draw["numbers"]:
            frequency[number] += 1
            scores[number] += recent_weight

    latest_numbers = set(draws[-1]["numbers"])
    for number in latest_numbers:
        scores[number] *= 0.82

    hot_numbers = sorted(scores, key=lambda number: scores[number], reverse=True)[:10]
    cold_numbers = sorted(scores, key=lambda number: scores[number])[:10]

    rng = random.SystemRandom()
    picks = []
    seen = set()
    max_attempts = set_count * 80
    attempts = 0

    while len(picks) < set_count and attempts < max_attempts:
        available = list(range(LOTTO_MIN_NUMBER, LOTTO_MAX_NUMBER + 1))
        weights = [scores[number] for number in available]
        selected = []

        while len(selected) < 6:
            chosen = rng.choices(available, weights=weights, k=1)[0]
            idx = available.index(chosen)
            available.pop(idx)
            weights.pop(idx)
            selected.append(chosen)

        selected.sort()
        key = tuple(selected)
        if key not in seen:
            picks.append(selected)
            seen.add(key)
        attempts += 1

    if len(picks) < set_count:
        pool = list(range(LOTTO_MIN_NUMBER, LOTTO_MAX_NUMBER + 1))
        while len(picks) < set_count:
            fallback = sorted(rng.sample(pool, 6))
            key = tuple(fallback)
            if key in seen:
                continue
            picks.append(fallback)
            seen.add(key)

    return {
        "recommended_sets": picks,
        "hot_numbers": sorted(hot_numbers),
        "cold_numbers": sorted(cold_numbers),
        "frequency": frequency,
    }


def _build_random_fallback(set_count):
    rng = random.SystemRandom()
    pool = list(range(LOTTO_MIN_NUMBER, LOTTO_MAX_NUMBER + 1))
    picks = []
    seen = set()

    while len(picks) < set_count:
        numbers = tuple(sorted(rng.sample(pool, 6)))
        if numbers in seen:
            continue
        picks.append(list(numbers))
        seen.add(numbers)

    return {
        "recommended_sets": picks,
        "hot_numbers": sorted(rng.sample(pool, 10)),
        "cold_numbers": sorted(rng.sample(pool, 10)),
    }


def _warmup_lotto_cache():
    global LOTTO_WARMUP_RUNNING
    try:
        loaded = _load_lotto_draws_from_backup()
        if loaded > 0:
            logger.info("Loaded lotto draws from backup source: %s", loaded)
            return

        latest_draw_no = _find_latest_draw_no()
        if latest_draw_no is None:
            return
        for draw_no in range(1, latest_draw_no + 1):
            _fetch_lotto_draw(draw_no)
    finally:
        with LOTTO_WARMUP_LOCK:
            LOTTO_WARMUP_RUNNING = False


def _ensure_lotto_warmup():
    global LOTTO_WARMUP_RUNNING
    with LOTTO_WARMUP_LOCK:
        if LOTTO_WARMUP_RUNNING:
            return
        LOTTO_WARMUP_RUNNING = True

    thread = threading.Thread(target=_warmup_lotto_cache, daemon=True)
    thread.start()


def lotto_predict_api(request):
    try:
        set_count = int(request.GET.get("sets", 5))
    except ValueError:
        return JsonResponse({"error": "sets 파라미터는 숫자여야 합니다."}, status=400)

    set_count = max(1, min(set_count, 10))
    _ensure_lotto_warmup()
    draws = sorted(LOTTO_DRAW_CACHE.values(), key=lambda item: item["draw_no"])

    if len(draws) < 30:
        fallback = _build_random_fallback(set_count)
        return JsonResponse(
            {
                "latest_draw_no": draws[-1]["draw_no"] if draws else "-",
                "latest_draw_date": draws[-1]["draw_date"] if draws else "-",
                "analyzed_draw_count": len(draws),
                "recommended_sets": fallback["recommended_sets"],
                "hot_numbers": fallback["hot_numbers"],
                "cold_numbers": fallback["cold_numbers"],
                "note": "전체 회차 데이터를 백그라운드 수집 중입니다. 잠시 후 다시 시도하세요.",
            }
        )

    prediction = _build_lotto_prediction(draws, set_count)
    return JsonResponse(
        {
            "latest_draw_no": draws[-1]["draw_no"],
            "latest_draw_date": draws[-1]["draw_date"],
            "analyzed_draw_count": len(draws),
            "recommended_sets": prediction["recommended_sets"],
            "hot_numbers": prediction["hot_numbers"],
            "cold_numbers": prediction["cold_numbers"],
            "note": "과거 패턴 기반 추천이며 당첨을 보장하지 않습니다.",
        }
    )
