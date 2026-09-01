"""Sprint G1: общие функции декодирования события TokenLaunched фабрик
pons.family -- вынесены из analysis/g1_verify_factory.py, чтобы
analysis/g1_graduation_events.py (полнопериодный счёт) не дублировал
уже юнит-тестированную логику. Layout события и адреса -- см.
data/pons_family/SOURCE.md.
"""
from __future__ import annotations

# Посчитано локально (Crypto.Hash.keccak) от точной сигнатуры типов в
# data/pons_family/PonsLaunchFactory_v1_abi.json -- см.
# data/pons_family/SOURCE.md за командой воспроизведения.
TOPIC0_TOKEN_LAUNCHED = "0xdb51ea9ad51ab453a65a4cb7e60c3cb378c9501bb002609f8f97778fb6c4235a"
TOPIC0_TOKEN_DEPLOYED = "0x1461370115e1c2be79cb529f8cfcbd11316e789d9c6099fc83417b0b4c48c62a"

FACTORY_V1 = "0xA5aAb3F0c6EeadF30Ef1D3Eb997108E976351feB"
FACTORY_V2 = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"


def decode_address_word(word_hex: str) -> str:
    h = str(word_hex).strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    h = h.rjust(64, "0")
    return "0x" + h[-40:]


def decode_uint_word(word_hex: str) -> int:
    h = str(word_hex).strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    return int(h, 16) if h else 0


def decode_token_launched(row: dict) -> dict:
    """row: tx_hash, block_number, block_time, topic1, topic2, topic3, data.
    TokenLaunched(token indexed, deployer indexed, dexFactory indexed,
    pairToken, pool, dexId, launchConfigId, positionId,
    restrictionsEndBlock, initialBuyAmount) -- см. data/pons_family/SOURCE.md.
    Валидирован юнит-тестом на синтетическом логе (см. историю коммитов
    Sprint G1) и на 25 реальных строках (run #7 recon)."""
    d = str(row["data"]).strip()
    if d.startswith("0x"):
        d = d[2:]
    words = [d[i:i + 64] for i in range(0, len(d), 64)]
    if len(words) != 7:
        raise ValueError(
            f"Ожидалось 7 слов по 32 байта в data (224 байта), получено {len(words)} "
            f"({len(d)} hex-символов) -- формат data не совпадает с ABI TokenLaunched. "
            f"tx={row['tx_hash']}"
        )
    return {
        "tx_hash": row["tx_hash"],
        "block_number": int(row["block_number"]),
        "block_time": row["block_time"],
        "token": decode_address_word(row["topic1"]),
        "deployer": decode_address_word(row["topic2"]),
        "dex_factory": decode_address_word(row["topic3"]),
        "pair_token": decode_address_word(words[0]),
        "pool": decode_address_word(words[1]),
        "dex_id": decode_uint_word(words[2]),
        "launch_config_id": decode_uint_word(words[3]),
        "position_id": decode_uint_word(words[4]),
        "restrictions_end_block": decode_uint_word(words[5]),
        "initial_buy_amount": decode_uint_word(words[6]),
    }


TOPIC0_POOL_GRADUATED = "0x0a44ef75df69c534f43cd6c1aa3ef8983065fe5fe79ef9e79f6494e6f258c259"


def decode_pool_graduated(row: dict) -> dict:
    """v2 PoolGraduated(token indexed, positionId, tokenAmount,
    pairTokenAmount) -- вынесено сюда из g1_v2_recon.py/g1_v2_postfilter.py
    (было продублировано в обоих) для использования в analysis/g1_pipeline.py,
    не меняя уже прогнанный и закоммиченный код."""
    d = str(row["data"]).strip()
    if d.startswith("0x"):
        d = d[2:]
    words = [d[i:i + 64] for i in range(0, len(d), 64)]
    if len(words) != 3:
        raise ValueError(f"Ожидалось 3 слова в data (96 байт), получено {len(words)}. tx={row['tx_hash']}")
    return {
        "tx_hash": row["tx_hash"],
        "block_number": int(row["block_number"]),
        "block_time": row["block_time"],
        "token": decode_address_word(row["topic1"]),
        "position_id": decode_uint_word(words[0]),
        "token_amount": decode_uint_word(words[1]),
        "pair_token_amount": decode_uint_word(words[2]),
    }


def fmt_ts(ts) -> str:
    """Timestamp (pd.Timestamp tz-aware/naive или сырая Dune-строка вида
    '2026-08-04 20:16:08.000 UTC') -> 'YYYY-MM-DD HH:MM:SS' БЕЗ суффикса
    offset/UTC -- Trino's `timestamp '...'` литерал не принимает offset в
    строке (см. history: баг пойман юнит-тестом до прогона на Dune, не
    после)."""
    import pandas as pd

    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_localize(None)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def estimate_seconds_per_block(decoded_rows: list[dict]) -> float:
    """Средний блок-тайм по соседним ЛОГАМ этой же выборки (не
    захардкожено) -- сортирует по block_number, берёт медиану дельт
    между строками с РАЗНЫМ block_number."""
    import pandas as pd

    rows = sorted(decoded_rows, key=lambda r: r["block_number"])
    deltas = []
    for a, b in zip(rows, rows[1:]):
        if b["block_number"] == a["block_number"]:
            continue
        t_a = pd.Timestamp(a["block_time"])
        t_b = pd.Timestamp(b["block_time"])
        dt = (t_b - t_a).total_seconds()
        dn = b["block_number"] - a["block_number"]
        if dn > 0 and dt > 0:
            deltas.append(dt / dn)
    if not deltas:
        raise ValueError(
            "Не удалось оценить секунд/блок -- в выборке недостаточно строк с разными "
            "block_number. Нужна более широкая выборка, не хардкодить константу."
        )
    deltas.sort()
    return deltas[len(deltas) // 2]  # медиана -- устойчивее среднего к редким выбросам
