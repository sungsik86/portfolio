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
LOTTO_STRATEGIES = {"conservative", "balanced", "aggressive"}
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
    return _build_lotto_prediction_with_penalty(draws, set_count, set(), "balanced")


def _passes_strategy_rules(selected_set, strategy):
    odd_count = sum(1 for number in selected_set if number % 2 == 1)
    total_sum = sum(selected_set)
    low = sum(1 for number in selected_set if number <= 15)
    mid = sum(1 for number in selected_set if 16 <= number <= 30)
    high = sum(1 for number in selected_set if number >= 31)

    if strategy == "conservative":
        if odd_count not in {2, 3, 4}:
            return False
        if not (95 <= total_sum <= 175):
            return False
        if min(low, mid, high) == 0:
            return False
    elif strategy == "aggressive":
        # 공격형은 제약을 완화해서 고점 번호를 더 시도한다.
        if odd_count not in {1, 2, 3, 4, 5}:
            return False
    else:  # balanced
        if odd_count not in {2, 3, 4}:
            return False
        if not (85 <= total_sum <= 190):
            return False

    return True


def _build_lotto_prediction_with_penalty(draws, set_count, user_numbers, strategy):
    scores = {number: 1.0 for number in range(LOTTO_MIN_NUMBER, LOTTO_MAX_NUMBER + 1)}
    frequency = {number: 0 for number in range(LOTTO_MIN_NUMBER, LOTTO_MAX_NUMBER + 1)}
    total_draws = len(draws)

    strategy_recent_multiplier = {
        "conservative": 1.15,
        "balanced": 1.0,
        "aggressive": 0.9,
    }.get(strategy, 1.0)
    strategy_user_penalty = {
        "conservative": 0.66,
        "balanced": 0.72,
        "aggressive": 0.8,
    }.get(strategy, 0.72)
    strategy_last_penalty = {
        "conservative": 0.74,
        "balanced": 0.82,
        "aggressive": 0.88,
    }.get(strategy, 0.82)

    for index, draw in enumerate(draws):
        recent_weight = (0.45 + ((index + 1) / total_draws) * 1.55) * strategy_recent_multiplier
        for number in draw["numbers"]:
            frequency[number] += 1
            scores[number] += recent_weight

    latest_numbers = set(draws[-1]["numbers"])
    for number in latest_numbers:
        scores[number] *= strategy_last_penalty
    for number in user_numbers:
        scores[number] *= strategy_user_penalty

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
        selected_set = set(selected)
        if len(selected_set & latest_numbers) >= 3:
            attempts += 1
            continue
        if user_numbers and len(selected_set & user_numbers) >= 3:
            attempts += 1
            continue
        if not _passes_strategy_rules(selected_set, strategy):
            attempts += 1
            continue

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


def _run_lotto_backtest(draws, strategy, user_numbers):
    if len(draws) < 120:
        return {
            "tested_draws": 0,
            "hit_3": 0,
            "hit_4": 0,
            "hit_5": 0,
            "hit_5_bonus": 0,
            "hit_6": 0,
            "hit_rate_3_plus": 0.0,
        }

    tested = min(60, len(draws) - 30)
    start_index = len(draws) - tested

    stats = {
        "tested_draws": tested,
        "hit_3": 0,
        "hit_4": 0,
        "hit_5": 0,
        "hit_5_bonus": 0,
        "hit_6": 0,
    }

    for idx in range(start_index, len(draws)):
        train_draws = draws[:idx]
        target = draws[idx]
        prediction = _build_lotto_prediction_with_penalty(train_draws, 1, user_numbers, strategy)
        pick = set(prediction["recommended_sets"][0])
        target_main = set(target["numbers"])
        match_count = len(pick & target_main)

        if match_count >= 3:
            stats["hit_3"] += 1
        if match_count >= 4:
            stats["hit_4"] += 1
        if match_count == 5:
            stats["hit_5"] += 1
            if target["bonus"] in pick:
                stats["hit_5_bonus"] += 1
        if match_count == 6:
            stats["hit_6"] += 1

    stats["hit_rate_3_plus"] = round((stats["hit_3"] / tested) * 100, 2) if tested else 0.0
    return stats


def _parse_number_set(raw_numbers):
    if not raw_numbers:
        return set()

    cleaned = raw_numbers.replace(",", " ").split()
    numbers = []
    for token in cleaned:
        try:
            number = int(token)
        except ValueError:
            raise ValueError("내 번호 형식이 올바르지 않습니다. 예: 3, 11, 17, 23, 31, 41") from None

        if number < LOTTO_MIN_NUMBER or number > LOTTO_MAX_NUMBER:
            raise ValueError("내 번호는 1~45 범위여야 합니다.")
        numbers.append(number)

    number_set = set(numbers)
    if len(number_set) == 0:
        return set()
    if len(number_set) > 6:
        raise ValueError("내 번호는 최대 6개까지 입력할 수 있습니다.")
    return number_set


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
    try:
        user_numbers = _parse_number_set(request.GET.get("my_numbers", ""))
    except ValueError as error:
        return JsonResponse({"error": str(error)}, status=400)
    strategy = request.GET.get("strategy", "balanced")
    if strategy not in LOTTO_STRATEGIES:
        return JsonResponse({"error": "strategy 값이 올바르지 않습니다."}, status=400)

    set_count = max(1, min(set_count, 10))
    _ensure_lotto_warmup()
    draws = sorted(LOTTO_DRAW_CACHE.values(), key=lambda item: item["draw_no"])
    latest_week_numbers = draws[-1]["numbers"] if draws else []

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
                "my_numbers": sorted(user_numbers),
                "last_week_numbers": latest_week_numbers,
                "strategy": strategy,
                "backtest": {
                    "tested_draws": 0,
                    "hit_3": 0,
                    "hit_4": 0,
                    "hit_5": 0,
                    "hit_5_bonus": 0,
                    "hit_6": 0,
                    "hit_rate_3_plus": 0.0,
                },
                "note": "전체 회차 데이터를 백그라운드 수집 중입니다. 잠시 후 다시 시도하세요.",
            }
        )

    prediction = _build_lotto_prediction_with_penalty(draws, set_count, user_numbers, strategy)
    backtest = _run_lotto_backtest(draws, strategy, user_numbers)
    return JsonResponse(
        {
            "latest_draw_no": draws[-1]["draw_no"],
            "latest_draw_date": draws[-1]["draw_date"],
            "analyzed_draw_count": len(draws),
            "recommended_sets": prediction["recommended_sets"],
            "hot_numbers": prediction["hot_numbers"],
            "cold_numbers": prediction["cold_numbers"],
            "my_numbers": sorted(user_numbers),
            "last_week_numbers": latest_week_numbers,
            "strategy": strategy,
            "backtest": backtest,
            "note": "전략/패널티 기반 통계 추천입니다. 백테스트는 최근 회차를 기준으로 계산됩니다.",
        }
    )
