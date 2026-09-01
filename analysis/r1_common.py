"""Sprint R1: общие декодеры/константы для событий Chainlink-фидов сток-
токенов -- см. docs/R1_DESIGN.md.
"""
from __future__ import annotations

# Посчитано локально (Crypto.Hash.keccak, тот же метод, что
# analysis/g1_common.py для событий pons.family -- см. воспроизведение
# ниже) от стандартной сигнатуры Chainlink-агрегатора
# AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt):
#
#   from Crypto.Hash import keccak
#   h = keccak.new(digest_bits=256)
#   h.update(b"AnswerUpdated(int256,uint256,uint256)")
#   "0x" + h.hexdigest()
#
# current и roundId -- indexed (topic1/topic2), updatedAt -- в data (не
# indexed). Совпадает с общеизвестным публичным значением для
# Chainlink-агрегаторов (кросс-чейн одна и та же сигнатура события).
TOPIC0_ANSWER_UPDATED = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"


def decode_answer_updated(row: dict) -> dict:
    """row: tx_hash, block_number, block_time, contract_address (адрес
    фида), topic1 (current, int256), topic2 (roundId, uint256), data
    (updatedAt, uint256, 32 байта)."""
    from g1_common import decode_uint_word

    current_raw = int(str(row["topic1"]), 16)
    # int256 -- если старший бит установлен, значение отрицательное
    # (дополнительный код); для цены фида отрицательные значения не
    # ожидаются, но декодируем корректно на случай стейл/аномальных данных.
    if current_raw >= 2 ** 255:
        current_raw -= 2 ** 256
    return {
        "tx_hash": row["tx_hash"],
        "block_number": int(row["block_number"]),
        "block_time": row["block_time"],
        "feed_address": row.get("contract_address"),
        "current": current_raw,
        "round_id": decode_uint_word(row["topic2"]),
        "updated_at": decode_uint_word(row["data"]),
    }
