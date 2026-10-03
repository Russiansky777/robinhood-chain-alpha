#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""USDC-НОГА ДЛЯ ТИПОВ, КУДА ИДУТ СИГНАЛЫ: Raydium CPMM, Raydium LaunchLab,
Pump AMM и кривая pump.fun v2 -- вход USDC вместо WSOL. Тот же флаг, та же тень.

ЗАЧЕМ. Счёт Code-1 за сутки: 118 сигналов с котировкой USDC, и лежат они НЕ там,
где закрыта USDC-нога. CPMM 44, LaunchLab 38, Pump AMM 25, кривая pump.fun 7 --
114 из 118; у закрытых сейчас типов (CLMM, DLMM, DAMM v2, Whirlpool) их 4.
Модуль analysis/c2_usdc_noga.py сделан для тех шести типов и про эти четыре не
знает вовсе: его таблица TYPES их не содержит, и storona() отвечает «тип пула не
в таблице USDC-ноги». Двухшаговый путь (bloom_lane_two_step) вторую ногу этих
типов СОБРАТЬ умеет -- кирпичами c2_swap_build, -- но у него нет НАШЕЙ таблицы
адресов, а без неё две ноги в 1232 байта не влезают (замер: 1326...1703 байта).
Поэтому здесь -- те же четыре шага, что у c2_usdc_noga, но для этих типов и с
нашей таблицей.

ЧУЖОГО КОДА НЕ ТРОНУТО. c2_usdc_noga.py правил Code-1 (четыре коммита 01--02.10),
c2_swap_build.py и bloom_lane_two_step.py -- общие модули полосы. Поэтому связка
сумм, порядок инструкций, предел CU, вид nonce и чаевые берутся ОТТУДА вызовами, а
не копируются: summy_nog -- c2_usdc_noga, запас первого шага и предел размера --
bloom_lane_two_step, инструкция ноги -- c2_swap_build. Самопроверка сверяет порядок
инструкций с порядком c2_usdc_noga ПОЗИЦИЯ В ПОЗИЦИЮ: если он там поменяется,
здесь это упадёт, а не разойдётся молча.

ЧТО СДЕЛАНО ПО КАЖДОМУ ТИПУ -- ЗАМЕРОМ, НЕ ПАМЯТЬЮ (числа в самопроверке):

  * Raydium CPMM. Вторая нога -- swap_base_input, 13 счетов. Байт в байт на 22
    живых сделках с КОТИРОВОЧНЫМ ТОКЕНОМ. Сделка источника может быть и ПРОДАЖЕЙ:
    у CPMM слоты стоят в порядке вход/выход, и стороны переставляются готовой
    функцией полосы (bloom_lane_two_step.перевернуть_cpmm) -- тогда нога
    собирается, а цена ОТКАЗЫВАЕТ по имени (цену покупки из обратного свопа брать
    нельзя). Отдельным отказом названо поле f > 1: в хранилище CPMM лежат не
    снятые protocol_fees/fund_fees, и у части сделок котировщик покупки обещает
    больше, чем даёт кривая по остаткам.
  * Raydium LaunchLab. buy_exact_in, 18 счетов. Байт в байт на 31 живой сделке с
    котировочным токеном. Цена -- c2_swap_build.launchlab_min_out: виртуальные
    резервы из события TradeEvent, они в единицах КОТИРОВКИ какой бы она ни была,
    нуль чтений.
  * Pump AMM. buy_exact_quote_in, 25 или 26 счетов. Байт в байт на 2 живых
    сделках с котировочным токеном (больше их в образцах нет). Цена -- по
    остаткам хранилищ, нуль чтений.
  * Кривая pump.fun v2. buy_exact_quote_in (дискриминатор c2ab1c46684d5b2f, 27
    счетов) -- та разновидность, у которой котировка ОТДЕЛЬНЫМ МИНТОМ
    (Global.whitelisted_quote_mints). 18-счётная v1 -- отказ по имени: там
    котировка нативный SOL и USDC-ноге делать нечего. Цена -- зеркало продажи
    (c3_kotirovka_prodazhi.вход_кривой) по КОТИРОВОЧНЫМ резервам события.
    Места 14, 15, 20 и 21 выводятся без чтений и без состояния: ATA базы, ATA
    котировки, накопитель объёма (PDA user_volume_accumulator + наш кошелёк) и
    СВЯЗАННЫЙ накопитель -- ATA этого накопителя по минту котировки. Последнее
    важно: в c2_swap_build про место 21 написано «ключ не выводится семенами,
    берётся из состояния», и для USDC такого состояния у нас не будет НИКОГДА
    (файл заполняется ошибками цепи). Замер на живой покупке
    3bn6wPKuLm62t7nqfK8u9iABz8zpAQQhLhznZiPSsRN8 (котировка HiMSSzzwkZ...)
    показал: место 21 РОВНО равно ATA(место 20, минт котировки, программа
    котировки). Поэтому здесь нога собирается своими местами, а Code-1 получает
    это пятью строками (см. docs/usdc_noga_signaly_kak_vstroit.md).

ЖИВЫХ ИНСТРУКЦИЙ С КОТИРОВКОЙ РОВНО USDC ПО ЭТИМ ЧЕТЫРЁМ ТИПАМ -- ОДНА ШТУКА, и
это сказано числом, а не обойдено. Сплошной обход 830 файлов data/ разбором
КАЖДОЙ инструкции (место минта котировки -- из раскладки типа) нашёл её ровно
одну: Pump AMM ZDadp1kxj3q46yU8..., пул PUMP/USDC, 25 счетов, внутри транзакции
роутера FLASHX8. Она пересобрана БАЙТ В БАЙТ ЦЕЛИКОМ и цена по ней посчиталась.
У CPMM, LaunchLab и кривой -- ноль. В сборе Code-2
(data/podbivka/usdc_noga_dlya_code3.json, 22 сигнала) их нет по построению: он
собирал «USDC в пулах НЕ CPMM и НЕ Pump AMM». В его же источнике
(data/podbivka/kotirovki_grupp_2026-10-01.json, 1090 сигналов) с котировкой USDC
лежат pump-amm 3 и raydium-cpmm 1, LaunchLab и кривой -- ноль. Поэтому механизм
проверен на живых сделках с КОТИРОВОЧНЫМ ТОКЕНОМ (не WSOL): это ТОТ ЖЕ путь кода
-- USDC в нём лишь значение минта, -- и ровно те места, что от минта котировки
зависят (наш ATA котировки, связанный накопитель), на них и проверены. ЧТО
ЗАПРОШЕНО У CODE-2: по 6+ живых USDC-свопов на каждый из четырёх типов
(buy с котировкой USDC, tx_jsonParsed + tx_base64), как он отдавал продажи в
data/samples/prodazhi/.

РАЗМЕР ПАКЕТА -- С НАШЕЙ ТАБЛИЦЕЙ АДРЕСОВ, И ЧИСЛО ЗДЕСЬ ВЕРХНЕЕ. Таблица
B1LxDr9ib1ezCSebbxnF73hCSAsVsy23U5fEwrqkfzXY создана Code-1 и сверена по цепи (34
адреса, data/usdc_noga_alt.json); в ней лежат программы ВСЕХ четырёх типов, минт
USDC, наш ATA USDC и все статичные счета первой ноги. Замер идёт без сети:
таблица подаётся замеру из файла репозитория. Образцы несут чужой котировочный
токен, и его минт, наш ATA под него и программа токена в таблице не лежат --
значит на USDC-котировке пакет будет МЕНЬШЕ измеренного, а не больше.

CU НЕ ВЫДУМАН И НЕ ИЗМЕРЕН ЗДЕСЬ. simulateTransaction -- не мой инструмент
(слово владельца), а у живых образцов в CU входит весь чужой маршрут (у этих
четырёх типов чистых сделок «только своп» в образцах нет вовсе: 88 869...541 083
CU на пакет источника). Поэтому предел берётся тот же, что у USDC-ноги:
c2_usdc_noga.cu_nogi() -- умолчание 250 000, переменная BLOOM_USDC_NOGA_CU, замер
Code-1 по цепи 158 733...172 545 CU на ДВЕ ноги. Точное число по этим четырём
типам даёт его прогон deploy/checks/usdc_noga_razmer_cu.py на хосте, когда придут
USDC-образцы.

РАЗМЕР МЕРИТСЯ В ЧЕТЫРЁХ РЕЖИМАХ, И РЕШАТЬ -- ПО САМОМУ ТЯЖЁЛОМУ. Первый замер
шёл с чаевыми на НАШ ЖЕ кошелёк, то есть по нижней границе: такой перевод нового
счёта в пакет не добавляет вовсе (нашла проверка 03.10 -- и права). Чужой адрес
чаевых стоит +32 байта, долговечный nonce -- ещё +2 счёта, вместе +106. В самом
тяжёлом режиме покупка влезает у CPMM (43 из 43), LaunchLab (35 из 35) и Pump AMM
(2 из 2) с запасом 70...100 байт, а у кривой НЕ ВЛЕЗАЕТ НИ ОДНА -- ей нужны
адреса в таблице полосы (четыре дают 1231 байт, то есть запас один байт; шесть --
1169, десять -- 1076).

ПРОДАЖА -- ТОКЕН -> USDC -> SOL, ВОСЕМЬ ИНСТРУКЦИЙ, ЗЕРКАЛО ПОКУПКИ. Первая нога
-- зеркало НАШЕЙ покупки токена (c3_prodavec_sborka), вторая -- зеркало НАШЕЙ
покупки первой ноги (та же покупка без мест 19 и 20). Вход второй ноги -- РОВНО
минимум первой. Минимум второй ноги только переданный: живые резервы пула
SOL/USDC этот модуль не читает. Размер в САМОМ ТЯЖЁЛОМ режиме: CPMM 1127...1158
влезает, а LaunchLab 1236...1267 и Pump AMM 1234...1265 НЕ влезают -- перебор
4...35 байт; лечится тем, что токеновый счёт не закрывается этой же транзакцией
(тогда 1197...1228 и 1195...1226, влезает всё). Кривая 1458...1490 -- нужны и
десять адресов в таблице, и отказ от закрытия (тогда 1202). Чего нет: живой
23-счётной продажи Pump AMM в образцах (раскладка снята с нашей 24-счётной), и у
Code-2 запрошено 6+ таких.

ЧЕГО ЗДЕСЬ НЕТ. Ни одной отправки, ни одной симуляции, ни одного чтения сети на
горячем пути, ни одной правки чужого файла. Врезка в полосу и круг 0.01 -- Code-1.
"""
from __future__ import annotations

import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

КОРЕНЬ = Path(__file__).resolve().parent.parent

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"

ROUTE = "usdc_noga_signaly"

PROG_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
PROG_LAUNCHLAB = "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"
PROG_PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PROG_KRIVAYA = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# ТИПЫ НЕ НАШЕЙ ТАБЛИЦЫ -- ТОЛЬКО ДЛЯ СВЕРКИ ПОРЯДКА РЕЗЕРВОВ (раздел 8а
# самопроверки): их котировщики живут в чужих модулях, и читатели у них общие.
PROG_CLMM_ = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
PROG_DLMM_ = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
PROG_DAMM2_ = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
PROG_DBC_ = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"

# СПОСОБ КОТИРОВКИ -- ПО ФАКТУ ТОГО, ЧЕМ СЧИТАЕТСЯ ЦЕНА, а не по типу пула:
#   rezervy   -- остатки хранилищ после сделки источника (x*y=k), нуль чтений;
#   launchlab -- виртуальные резервы события TradeEvent, нуль чтений;
#   krivaya_v2 -- кривая pump.fun по КОТИРОВОЧНЫМ резервам события, нуль чтений.
SPOSOB_REZERVY = "rezervy"
SPOSOB_LAUNCHLAB = "launchlab"
SPOSOB_KRIVAYA_V2 = "krivaya_v2"

# ЧИСЛА СИГНАЛОВ -- СЧЁТ CODE-1 ЗА СУТКИ (слово владельца 03.10). Они здесь не
# для арифметики, а чтобы порядок типов в таблице совпадал с порядком денег.
SIGNALOV_V_SUTKI = {PROG_CPMM: 44, PROG_LAUNCHLAB: 38, PROG_PUMP_AMM: 25,
                    PROG_KRIVAYA: 7}

TIPY = {
    PROG_CPMM: {"label": "Raydium CPMM", "ix": "swap_base_input",
                "kotirovshchik": SPOSOB_REZERVY, "perevorot": True},
    PROG_LAUNCHLAB: {"label": "Raydium LaunchLab", "ix": "buy_exact_in",
                     "kotirovshchik": SPOSOB_LAUNCHLAB, "perevorot": False},
    PROG_PUMP_AMM: {"label": "Pump AMM", "ix": "buy_exact_quote_in",
                    "kotirovshchik": SPOSOB_REZERVY, "perevorot": False},
    PROG_KRIVAYA: {"label": "Кривая pump.fun v2", "ix": "buy_exact_quote_in_v2",
                   "kotirovshchik": SPOSOB_KRIVAYA_V2, "perevorot": False},
}

# КРИВАЯ v2: РАЗНОВИДНОСТЬ И МЕСТА. Дискриминатор -- из живых сделок (его же
# знает c2_swap_build.BONDING_DISCS), 27 счетов, котировка отдельным минтом.
KRIVAYA_V2_DISC = "c2ab1c46684d5b2f"
KRIVAYA_V2_SCHETOV = 27
# Места, которые МЫ подставляем, и чем они выводятся. Проверено на живых сделках:
#   13 -- наш кошелёк (подписант)
#   14 -- ATA(наш, минт базы (место 1), программа базы (место 3))
#   15 -- ATA(наш, минт котировки (место 2), программа котировки (место 4))
#   20 -- PDA(["user_volume_accumulator", наш], программа кривой)
#   21 -- ATA(место 20, минт котировки, программа котировки)
KRIVAYA_V2_MESTA = {"user": 13, "ata_bazy": 14, "ata_kotirovki": 15,
                    "uva": 20, "assoc_uva": 21,
                    "mint_bazy": 1, "mint_kotirovki": 2,
                    "prog_bazy": 3, "prog_kotirovki": 4}
SEMYA_UVA = b"user_volume_accumulator"

# ПОЛУЧАТЕЛИ КОМИССИЙ КРИВОЙ -- СПИСКИ ИЗ ПУБЛИЧНЫХ ДОКУМЕНТОВ ПРОГРАММЫ, НЕ ИЗ
# ПАМЯТИ. Источник: pump-fun/pump-public-docs, docs/FEE_RECIPIENTS.md («There are
# 24 fee recipient addresses in total: 8 normal ... 8 reserved ... 8 buyback»).
# Раскладка счёта Global в IDL это подтверждает числом: fee_recipient + 
# fee_recipients[7] = 8, reserved_fee_recipient + reserved_fee_recipients[7] = 8,
# buyback_fee_recipients[8].
#
# ОГРАНИЧЕНИЕ НА СЧЁТЕ -- НЕ ВЫВОД АДРЕСА, А ПРИНАДЛЕЖНОСТЬ СПИСКУ. В IDL у счёта
# fee_recipient (место 6) НЕТ ни pda, ни relations -- только writable; значит
# адрес не выводится семенами и проверяется ВНУТРИ программы по Global. Ошибки
# программы это называют: 6057 BuybackFeeRecipientNotAuthorized, 6029
# UnsortedNotUniqueFeeRecipients, 6061 «buyback fee recipients require exactly 8
# remaining accounts». А вот их ATA (места 7 и 9) в IDL pda-выводимые:
# seeds = [получатель, quote_token_program, quote_mint] под ATA-программой.
# ЗАМЕР ПОДТВЕРЖДАЕТ, ЧТО ПРИНИМАЕТСЯ ЛЮБОЙ ИЗ СПИСКА: на шести живых v2-покупках
# на месте 6 стояли ТРИ разных обычных получателя, на месте 8 -- ЧЕТЫРЕ разных
# buyback, и все шесть сделок сели.
POLUCHATELI_OBYCHNYE = (
    "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV",
    "7VtfL8fvgNfhz17qKRMjzQEXgbdpnHHHQRh54R9jP2RJ",
    "7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX",
    "9rPYyANsfQZw3DnDmKE3YCQF5E8oD89UXoHn9JFEhJUz",
    "AVmoTthdrX6tKt4nDjco2D775W2YK3sDhxPcMmzUAmTY",
    "CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM",
    "FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz",
    "G5UZAVbAf46s7cKWoyKu8kYTip9DGTpbLZ2qa9Aq69dP",
)
POLUCHATELI_MAYHEM = (
    "GesfTA3X2arioaHp8bbKdjG9vJtskViWACZoYvxp4twS",
    "4budycTjhs9fD6xw62VBducVTNgMgJJ5BgtKq7mAZwn6",
    "8SBKzEQU4nLSzcwF4a74F2iaUDQyTfjGndn6qUWBnrpR",
    "4UQeTP1T39KZ9Sfxzo3WR5skgsaP6NZa87BAkuazLEKH",
    "8sNeir4QsLsJdYpc9RZacohhK1Y5FLU3nC5LXgYB4aa6",
    "Fh9HmeLNUMVCvejxCtCL2DbYaRyBFVJ5xrWkLnMH6fdk",
    "463MEnMeGyJekNZFQSTUABBEbLnvMTALbT6ZmsxAbAdq",
    "6AUH3WEHucYZyC61hqpqYUWVto5qA5hjHuNQ32GNnNxA",
)
POLUCHATELI_BUYBACK = (
    "5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD",
    "9M4giFFMxmFGXtc3feFzRai56WbBqehoSeRE5GK7gf7",
    "GXPFM2caqTtQYC2cJ5yJRi9VDkpsYZXzYdwYpGnLmtDL",
    "3BpXnfJaUTiwXnJNe7Ej1rcbzqTTQUvLShZaWazebsVR",
    "5cjcW9wExnJJiqgLjq7DEG75Pm6JBgE1hNv4B2vHXUW6",
    "EHAAiTxcdDwQ3U4bU6YcMsQGaekdzLS3B5SmYo46kJtL",
    "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD",
    "A7hAgCzFw14fejgCp387JUJRMNyz4j89JKnhtKU8piqW",
)
# НАШ ВЫБОР -- ОДИН И ВСЕГДА (слово владельца 03.10). Выбраны не наугад: оба уже
# ЛЕЖАТ в таблице адресов полосы (data/usdc_noga_alt.json) как счета ноги
# SOL -> USDC, то есть на размер пакета сами по себе не добавляют ни байта, и
# оба ВИДЕНЫ ЖИВЫМИ на цепи в этих ролях. Для mayhem-монет обычный получатель не
# годится (нужен из reserved), и там взят первый из списка: живой сделки с
# mayhem у нас нет ни одной -- так и сказано.
POLUCHATEL_NASH = "7hTckgnGnLQR6sdH7YkqFTAA7VwTfYFaZ6EhEsU3saCX"
BUYBACK_NASH = "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"
POLUCHATEL_NASH_MAYHEM = POLUCHATELI_MAYHEM[0]
KRIVAYA_V2_MESTA_POLUCHATELEJ = {"poluchatel": 6, "ata_poluchatelya": 7,
                                 "buyback": 8, "ata_buyback": 9}
WHY_POLUCHATEL_NE_V_SPISKE = ("получатель комиссии не из списка программы "
                              "(docs/FEE_RECIPIENTS.md) -- программа такой "
                              "счёт не примет")
# ТИПЫ, КОТОРЫЕ ЕСТЬ И У c2_usdc_noga. 03.10 Code-1 добавил туда CPMM, и это
# названное пересечение, а не случай: покупку CPMM берёт его путь (врезка зовёт
# его первым), здесь CPMM нужен ради переворота сторон и ради продажи.
PERESECHENIE_S_UN = (PROG_CPMM,)

WHY_TIP = "тип пула не в таблице USDC-ноги сигнальных типов"
WHY_KRIVAYA_V1 = ("кривая pump.fun без котировочного минта (18 счетов): котировка "
                  "там нативный SOL, и второй ноге в USDC делать нечего")
WHY_KRIVAYA_DISC = ("разновидность инструкции кривой не buy_exact_quote_in_v2 "
                    "(c2ab1c46684d5b2f) -- USDC-нога берёт только её")
WHY_OTHER_SIDE = ("своп источника шёл в другую сторону -- цену покупки "
                  "из обратного свопа брать нельзя")
WHY_NO_REVERSE = ("сделка источника -- продажа, а переставить стороны у этого типа "
                  "нечем (нога идёт кирпичами c2_swap_build)")
WHY_F_BOLSHE_1 = ("котировщик покупки обещает больше, чем даёт кривая по остаткам "
                  "хранилищ (f > 1): в хранилище CPMM лежат не снятые "
                  "protocol_fees/fund_fees, и минимум выхода вышел бы завышенным")
WHY_EXACT_OUT = "шаг 2 с инструкцией точного выхода -- этим путём не берём"
WHY_SOBYTIYA_NET = "события сделки кривой в транзакции источника нет или оно не разобралось"
WHY_KOTIROVKA_SOBYTIYA = ("минт котировки в событии кривой не тот, которым платит "
                          "нога -- считать по нему нельзя")

# НАША ТАБЛИЦА АДРЕСОВ ПОЛОСЫ. Создана и сверена по цепи Code-1 (lane_usdc_alt),
# здесь только читается из файла: ни одного вызова сети.
FAJL_TABLICY = "usdc_noga_alt.json"
TABLICA_ADRESOV = "B1LxDr9ib1ezCSebbxnF73hCSAsVsy23U5fEwrqkfzXY"
# 42, А НЕ 34: 03.10 Code-1 ДОЛИЛ в ту же таблицу восемь адресов кривой живьём
# (таблица умеет расширяться -- доливается разница, а не весь список). Число
# стоит проверкой, а не догадкой: вырастет таблица ещё -- самопроверка скажет
# ЧИСЛОМ, а не промолчит, потому что от него зависят ВСЕ замеры размера.
# ЧИСЛО ЖИВОЕ, А НЕ ПАМЯТНОЕ. 03.10 в 17:24Z таблица расширена живьём по
# набору Code-3: было 42, долито 5 (ATA получателя и buyback по USDC,
# получатель mayhem и две его ATA), подпись 2ADKYh1T..., рента по цепи
# 0.0008178 SOL. Стало 47.
ADRESOV_V_TABLICE = 47


class OshibkaNogi(Exception):
    pass


def _moduli():
    import bloom_lane_two_step as TS  # noqa: PLC0415
    import c2_common as C  # noqa: PLC0415
    import c2_shadow_build as SB  # noqa: PLC0415
    import c2_swap_build as B  # noqa: PLC0415
    import c2_usdc_noga as UN  # noqa: PLC0415
    import c3_kotirovka_prodazhi as K  # noqa: PLC0415
    return C, B, UN, TS, SB, K


def tip_znakom(programma: str) -> bool:
    return (programma or "") in TIPY


def kotirovshchik(programma: str) -> str | None:
    return (TIPY.get(programma or "") or {}).get("kotirovshchik")


def rezhim(gruppa: str | None = None) -> str:
    """Режим -- ТОТ ЖЕ, что у USDC-ноги: один флаг на обе таблицы типов."""
    return _moduli()[2].rezhim(gruppa)


def cu_nogi() -> int:
    return _moduli()[2].cu_nogi()


def tablica_polosy(fajl: str | None = None) -> dict:
    """34 адреса нашей таблицы из файла репозитория. Без сети."""
    put_ = Path(fajl) if fajl else (КОРЕНЬ / "data" / FAJL_TABLICY)
    iz = {"ok": False, "why_not": None, "klyuch": None, "adresa": None}
    if not put_.exists():
        iz["why_not"] = f"файла таблицы нет: {put_}"
        return iz
    d = json.loads(put_.read_text(encoding="utf-8"))
    a = ((d.get("адреса") or {}).get("адреса")) or []
    # КЛЮЧ ТАБЛИЦЫ ЛЕЖИТ ДВУМЯ ВИДАМИ, И ЧИТАЮТСЯ ОБА. 03.10 Code-1 научил
    # таблицу РАСШИРЯТЬСЯ (доливается разница, а не весь список) -- и ключ
    # переехал из "принятие.состояние.адрес" в "расширение.адрес". Третьего вида
    # не изобретаю, но и падать на смене вида файла нечем: тогда замер встал бы
    # целиком из-за одного ключа.
    klyuch = ((((d.get("принятие") or {}).get("состояние") or {}).get("адрес"))
              or ((d.get("расширение") or {}).get("адрес")))
    if not a or not klyuch:
        iz["why_not"] = "в файле таблицы нет ни адресов, ни ключа"
        return iz
    iz.update(ok=True, klyuch=klyuch, adresa=list(a))
    return iz


def lut_polosy(fajl: str | None = None):
    """Таблица адресов объектом solders -- для замера размера БЕЗ сети."""
    t = tablica_polosy(fajl)
    if not t["ok"]:
        raise OshibkaNogi(t["why_not"])
    from solders.address_lookup_table_account import AddressLookupTableAccount  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    return AddressLookupTableAccount(
        Pubkey.from_string(t["klyuch"]),
        [Pubkey.from_string(x) for x in t["adresa"]])


# ------------------------------------------------- сторона USDC и шаблон ноги

def storona(tx_istochnika: dict, *, programma: str, pul: str | None = None,
            hranilishche: str | None = None, mint_kotirovki: str = USDC) -> dict:
    """Шаблон второй ноги, развёрнутый входом на USDC. Ни одного чтения сети.

    Вид ответа -- РОВНО как у c2_usdc_noga.storona (ok, why_not, way, label, tpl,
    base_mint, base_program, quote_program, razvernut, exact_out, src_tpl), чтобы
    врезка в полосу была одна на две таблицы типов, а не две разные.
    """
    C, B, UN, TS, _SB, _K = _moduli()
    iz = {"ok": False, "why_not": None, "way": UN.WAY_BRICKS, "label": None,
          "tpl": None, "base_mint": None, "base_program": None,
          "quote_program": None, "razvernut": False, "exact_out": None,
          "src_tpl": None, "kotirovshchik": None, "raznovidnost": None}
    t = TIPY.get(programma or "")
    if not t:
        iz["why_not"] = f"{WHY_TIP}: {str(programma)[:8]}"
        return iz
    iz["label"], iz["kotirovshchik"] = t["label"], t["kotirovshchik"]
    selektor = hranilishche or pul
    if not selektor:
        iz["why_not"] = "ни хранилища, ни адреса пула -- инструкцию ноги не выбрать"
        return iz
    shab = B.extract_template(tx_istochnika, programma, selektor)
    if not shab.get("ok"):
        iz["why_not"] = f"шаблон ноги: {shab.get('why_not')}"
        return iz
    iz["src_tpl"] = shab
    # КРИВАЯ: ТОЛЬКО РАЗНОВИДНОСТЬ С КОТИРОВОЧНЫМ МИНТОМ. Спутать её с 18-счётной
    # значило бы подставить наш кошелёк не туда.
    if programma == PROG_KRIVAYA:
        iz["raznovidnost"] = shab.get("ix")
        if shab.get("ix") != KRIVAYA_V2_DISC:
            iz["why_not"] = (WHY_KRIVAYA_V1 if len(shab["accounts"]) == 18
                             else WHY_KRIVAYA_DISC)
            return iz
        if len(shab["accounts"]) != KRIVAYA_V2_SCHETOV:
            iz["why_not"] = (f"счетов {len(shab['accounts'])}, а у "
                             f"buy_exact_quote_in_v2 их {KRIVAYA_V2_SCHETOV}")
            return iz
    tochnyj = (shab.get("exact_out") if shab.get("exact_out") is not None
               else (B.spec_of(shab) or {}).get("exact_out"))
    iz["exact_out"] = bool(tochnyj)
    if tochnyj:
        iz["why_not"] = WHY_EXACT_OUT
        return iz
    mv = B.mints_and_vaults(shab, tx_istochnika)
    if not mv:
        iz["why_not"] = "у пула источника не разобрались минты и хранилища"
        return iz
    if mv.get("quote_mint") == mint_kotirovki:
        iz.update(ok=True, tpl=shab, base_mint=mv.get("base_mint"),
                  base_program=mv.get("base_program"),
                  quote_program=mv.get("quote_program"), razvernut=False)
        return iz
    # СТОРОНЫ НАОБОРОТ. У CPMM слоты стоят в порядке ВХОД/ВЫХОД (замер полосы на
    # 34 инструкциях из 31 пула), и переставить их умеет готовая функция
    # bloom_lane_two_step.перевернуть_cpmm -- своей здесь не заводится. У
    # остальных трёх типов места именованы (база/котировка), и переставлять их
    # нечем: это ДРУГАЯ инструкция программы.
    if mv.get("base_mint") != mint_kotirovki:
        iz["why_not"] = (f"котировка пула не {mint_kotirovki[:8]}: стороны пула "
                         f"{str(mv.get('quote_mint'))[:8]}/"
                         f"{str(mv.get('base_mint'))[:8]}")
        return iz
    if not t.get("perevorot"):
        iz["why_not"] = WHY_NO_REVERSE
        return iz
    # ПЕРЕВОРОТ ЧУЖОЙ ФУНКЦИЕЙ, И ЕГО ОТКАЗ -- ИСКЛЮЧЕНИЕ, А НЕ ПОЛЕ. Она
    # проверяет сама себя: после перестановки на месте 10 обязан стоять ВХОД,
    # иначе ValueError. Ловим и называем словами, наружу исключение не пускаем.
    try:
        perev = TS.перевернуть_cpmm(shab, mint_kotirovki)
    except (ValueError, KeyError, IndexError) as exc:
        iz["why_not"] = f"стороны CPMM не переставились: {exc}"
        return iz
    mv2 = B.mints_and_vaults(perev, tx_istochnika)
    if not mv2 or mv2.get("quote_mint") != mint_kotirovki:
        iz["why_not"] = ("после перестановки сторон CPMM котировка всё равно не "
                         f"{mint_kotirovki[:8]}")
        return iz
    iz.update(ok=True, tpl=perev, base_mint=mv2.get("base_mint"),
              base_program=mv2.get("base_program"),
              quote_program=mv2.get("quote_program"), razvernut=True)
    return iz


# --------------------------------------------- места второй ноги у кривой v2

def mesta_krivoj_v2(scheta: list, *, nash_koshelek: str,
                    nashi_poluchateli: bool = False,
                    mayhem: bool = False) -> dict:
    """{место: адрес} для НАШИХ счетов buy_exact_quote_in_v2. Нуль чтений.

    Связанный накопитель (место 21) здесь ВЫВОДИТСЯ, а не берётся из состояния:
    в c2_swap_build про него написано «ключ не выводится семенами», и для USDC
    такого состояния у нас не будет никогда (файл заполняется ошибками цепи).
    Два независимых источника говорят обратное:
      * ПУБЛИЧНЫЙ IDL (pump-fun/pump-public-docs, idl/pump.json,
        buy_exact_quote_in_v2, счёт 21 associated_user_volume_accumulator):
        pda.seeds = [account user_volume_accumulator, account
        quote_token_program, account quote_mint], program -- константа, равная
        ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL. Это и есть вывод ATA;
      * ЗАМЕР: на трёх живых покупках с котировочными минтами (HiMSSzzwkZ...,
        3NZ9JMVBmGAq..., XspzcW1PRtgf...) место 21 равно ATA(место 20, минт
        котировки, программа котировки) РОВНО, 5 мест из 5 на каждой.
    У sell_v2 эти же счета стоят на местах 19 и 20 (место gva покупки выпадает).
    """
    _C, B, _UN, _TS, _SB, _K = _moduli()
    M = KRIVAYA_V2_MESTA
    iz = {"ok": False, "why_not": None, "mesta": None, "soshlos": 0, "provereno": 0,
          "plohie": None}
    if len(scheta) != KRIVAYA_V2_SCHETOV:
        iz["why_not"] = f"счетов {len(scheta)}, ожидалось {KRIVAYA_V2_SCHETOV}"
        return iz
    mint_b, mint_q = scheta[M["mint_bazy"]], scheta[M["mint_kotirovki"]]
    prog_b, prog_q = scheta[M["prog_bazy"]], scheta[M["prog_kotirovki"]]
    uva = B.pda([SEMYA_UVA, "USER"], nash_koshelek, PROG_KRIVAYA)
    mesta = {M["user"]: nash_koshelek,
             M["ata_bazy"]: B.ata(nash_koshelek, mint_b, prog_b),
             M["ata_kotirovki"]: B.ata(nash_koshelek, mint_q, prog_q),
             M["uva"]: uva,
             M["assoc_uva"]: B.ata(uva, mint_q, prog_q)}
    # ПОЛУЧАТЕЛИ КОМИССИЙ -- НАШИ И ВСЕГДА ОДНИ (слово владельца 03.10), но
    # ТОЛЬКО в НАШЕЙ сборке. При пересборке сделки источника они остаются его:
    # иначе самопроверка «восстанови его инструкцию точно» стала бы ложной.
    if nashi_poluchateli:
        P = KRIVAYA_V2_MESTA_POLUCHATELEJ
        пол = POLUCHATEL_NASH_MAYHEM if mayhem else POLUCHATEL_NASH
        список = POLUCHATELI_MAYHEM if mayhem else POLUCHATELI_OBYCHNYE
        if пол not in список or BUYBACK_NASH not in POLUCHATELI_BUYBACK:
            iz["why_not"] = (f"{WHY_POLUCHATEL_NE_V_SPISKE}: "
                             f"{пол[:8]} / {BUYBACK_NASH[:8]}")
            return iz
        mesta[P["poluchatel"]] = пол
        mesta[P["ata_poluchatelya"]] = B.ata(пол, mint_q, prog_q)
        mesta[P["buyback"]] = BUYBACK_NASH
        mesta[P["ata_buyback"]] = B.ata(BUYBACK_NASH, mint_q, prog_q)
        iz["nashi_poluchateli"] = {"poluchatel": пол, "buyback": BUYBACK_NASH,
                                   "mayhem": bool(mayhem)}
    iz["mesta"] = mesta
    # ПЕРЕСБОРКА СДЕЛКИ ИСТОЧНИКА -- ЭТО И ЕСТЬ ПРОВЕРКА ВЫВОДА. Когда кошелёк тот
    # же, все пять мест обязаны совпасть с его счетами; расхождение -- отказ.
    if scheta[M["user"]] == nash_koshelek and not nashi_poluchateli:
        plohie = [i for i, a in mesta.items() if scheta[i] != a]
        iz.update(soshlos=len(mesta) - len(plohie), provereno=len(mesta),
                  plohie=plohie)
        if plohie:
            iz["why_not"] = (f"вывод мест не сошёлся с покупкой источника: "
                             f"{plohie}")
            return iz
    iz["ok"] = True
    return iz


def instrukciya_nogi(storona_nogi: dict, *, user: str, amount_in: int, min_out: int,
                     tx_istochnika: dict | None = None,
                     kak_u_istochnika: bool = False, mayhem: bool = False):
    """Инструкция свопа второй ноги. У кривой v2 -- своими местами, у остальных --
    кирпич c2_swap_build.swap_instruction (его же зовёт c2_usdc_noga).

    kak_u_istochnika=True -- пересборка сделки источника для самопроверки байт в
    байт: его аргументы, его хвост, его пользователь.
    """
    _C, B, UN, _TS, _SB, _K = _moduli()
    if not (storona_nogi or {}).get("ok"):
        raise OshibkaNogi((storona_nogi or {}).get("why_not") or "стороны ноги нет")
    tpl = storona_nogi["tpl"]
    if tpl.get("program") != PROG_KRIVAYA:
        return UN.instrukciya_nogi(storona_nogi, user=user, amount_in=amount_in,
                                   min_out=min_out, tx_istochnika=tx_istochnika or {},
                                   kak_u_istochnika=kak_u_istochnika)
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    pr = mesta_krivoj_v2(tpl["accounts"], nash_koshelek=user,
                         nashi_poluchateli=not kak_u_istochnika,
                         mayhem=bool(mayhem))
    if not pr["ok"]:
        raise OshibkaNogi(pr["why_not"])
    subs = pr["mesta"]
    metas = []
    for i, a in enumerate(tpl["accounts"]):
        adres = subs.get(i, a)
        metas.append(AccountMeta(Pubkey.from_string(adres),
                                 is_signer=(adres == user),
                                 is_writable=(i in subs
                                              or tpl["writable"].get(a, False))))
    # ДИСКРИМИНАТОР И ХВОСТ -- ИЗ СДЕЛКИ ИСТОЧНИКА, как у кирпича: имени
    # разновидности мы не знаем, а угадывать его на деньгах нельзя.
    data = tpl["data"][:8] + struct.pack("<QQ", int(amount_in), int(min_out)) \
        + tpl["data"][24:]
    return Instruction(Pubkey.from_string(tpl["program"]), data, metas)


def polzovatel_istochnika(storona_nogi: dict, tx_istochnika: dict) -> str | None:
    """Кто подписал сделку источника на его месте в этой инструкции."""
    _C, _B, UN, _TS, _SB, _K = _moduli()
    tpl = (storona_nogi or {}).get("tpl") or {}
    if tpl.get("program") == PROG_KRIVAYA:
        сч = tpl.get("accounts") or []
        return сч[KRIVAYA_V2_MESTA["user"]] if len(сч) == KRIVAYA_V2_SCHETOV else None
    return UN.polzovatel_istochnika(storona_nogi, tx_istochnika)


# ------------------------------------------------------------ цена и минимум

def sobytie_krivoj(tx_istochnika: dict, mint_bazy: str | None = None) -> dict:
    """Событие сделки кривой с КОТИРОВОЧНОЙ стороной. Разбор -- свой же модуль
    котировки продажи (c3_kotirovka_prodazhi.событие_кривой): он читает весь
    хвост v2 по IDL и не берёт событие, которое не воспроизвело свою сделку."""
    _C, _B, _UN, _TS, _SB, K = _moduli()
    iz = {"ok": False, "why_not": None, "polya": None, "tokenovaya": None,
          "quote_mint": None}
    ev = K.событие_кривой(tx_istochnika or {}, mint_bazy)
    if not ev.get("ok"):
        iz["why_not"] = f"{WHY_SOBYTIYA_NET}: {ev.get('why_not')}"
        return iz
    p = ev["поля"]
    tok = int(p.get("sol_amount") or 0) == 0 and int(p.get("quote_amount") or 0) > 0
    iz.update(ok=True, polya=p, tokenovaya=tok,
              quote_mint=(p.get("quote_mint") if tok else WSOL))
    return iz


def min_out_boj(storona_nogi: dict, tx_istochnika: dict, *, amount_in: int,
                proskalzyvanie: float, mint_kotirovki: str = USDC) -> dict:
    """Минимум выхода второй ноги -- котировщиком типа, НУЛЬ ЧТЕНИЙ СЕТИ.

    Цена у всех четырёх типов считается из состояния ПОСЛЕ сделки источника: у
    CPMM и Pump AMM -- остатки хранилищ, у LaunchLab и кривой -- виртуальные
    резервы их события. Своего объёма в цене нет ни у одного (пул её не знает до
    сделки), и это закрывает проскальзывание группы, а не этот модуль.
    """
    _C, B, _UN, _TS, _SB, K = _moduli()
    iz = {"ok": False, "why_not": None, "min_out": None, "expected_out": None,
          "put": None, "chtenij": 0, "fee_share": None, "f": None}
    if not (storona_nogi or {}).get("ok"):
        iz["why_not"] = (storona_nogi or {}).get("why_not") or "стороны ноги нет"
        return iz
    tpl = storona_nogi["tpl"]
    sp = kotirovshchik(tpl.get("program") or "")
    iz["put"] = sp
    if not isinstance(amount_in, int) or amount_in <= 0:
        iz["why_not"] = f"сумма второй ноги не положительное целое: {amount_in!r}"
        return iz
    if sp == SPOSOB_REZERVY:
        if storona_nogi.get("razvernut"):
            iz["why_not"] = WHY_OTHER_SIDE
            return iz
        mo = B.min_out_from_reserves(tpl, tx_istochnika, int(amount_in),
                                     float(proskalzyvanie))
        mo = mo if isinstance(mo, dict) else {}
        iz["f"] = mo.get("fee_factor")
        iz["fee_share"] = (1 - float(mo["fee_factor"])
                           if isinstance(mo.get("fee_factor"), (int, float)) else None)
        if not mo.get("ok"):
            iz["why_not"] = mo.get("why_not")
            return iz
        if mo.get("f_vyshe_krivoj"):
            iz["why_not"] = f"{WHY_F_BOLSHE_1}: f = {float(mo['fee_factor']):.6f}"
            return iz
        iz.update(ok=True, min_out=int(mo["min_out"]),
                  expected_out=int(mo["expected_out"]))
        return iz
    if sp == SPOSOB_LAUNCHLAB:
        mo = B.launchlab_min_out(tx_istochnika, int(amount_in), float(proskalzyvanie))
        mo = mo if isinstance(mo, dict) else {}
        if not mo.get("ok"):
            iz["why_not"] = mo.get("why_not")
            return iz
        iz.update(ok=True, min_out=int(mo["min_out"]),
                  expected_out=int(mo["expected_out"]),
                  fee_share=mo.get("fee_rate"))
        return iz
    if sp == SPOSOB_KRIVAYA_V2:
        ev = sobytie_krivoj(tx_istochnika, storona_nogi.get("base_mint"))
        if not ev["ok"]:
            iz["why_not"] = ev["why_not"]
            return iz
        if not ev["tokenovaya"] or ev["quote_mint"] != mint_kotirovki:
            iz["why_not"] = (f"{WHY_KOTIROVKA_SOBYTIYA}: в событии "
                             f"{str(ev['quote_mint'])[:8]}, нога платит "
                             f"{mint_kotirovki[:8]}")
            return iz
        p = ev["polya"]
        k = K.вход_кривой(котировки=int(amount_in),
                          вирт_котировка=int(p["virtual_quote_reserves"]),
                          вирт_токены=int(p["virtual_token_reserves"]),
                          fee_bps=int(p["fee_basis_points"]),
                          creator_bps=int(p["creator_fee_basis_points"]))
        iz["fee_share"] = (int(p["fee_basis_points"])
                           + int(p["creator_fee_basis_points"])) / 10_000
        if not k.get("ok"):
            iz["why_not"] = f"кривая покупки отказала: {k.get('why_not')}"
            return iz
        ozh = int(k["выход"])
        iz.update(ok=True, expected_out=ozh,
                  min_out=int(ozh * (1 - float(proskalzyvanie))),
                  do_krivoj=k.get("до_кривой"))
        return iz
    iz["why_not"] = f"котировщика для {str(tpl.get('program'))[:8]} в таблице нет"
    return iz


def summy_nog(**kw) -> dict:
    """Связка сумм двух ног -- числами c2_usdc_noga (а те -- двухшагового пути)."""
    return _moduli()[2].summy_nog(**kw)


# ------------------------------------------------------------------ тень

def ten(*, tx_istochnika: dict, istochnik: str, mint: str, lamporty: int,
        kesh_nog, proskalzyvanie: float = 0.35, gruppa: str | None = None,
        nalog_kotirovki_bps=None, mint_kotirovki: str = USDC) -> dict:
    """ЧИСЛА ноги без транзакции и без сети: годилось бы или нет, и почему.

    Вид ответа -- РОВНО как у c2_usdc_noga.ten, чтобы поля журнала решений у двух
    таблиц типов совпадали и врезка была одна на обе. Решений не принимает:
    транзакции здесь нет вовсе, и в тени полоса ничего не отправляет.

    ОТЛИЧИЕ ОДНО, И ОНО В ПОЛЬЗУ ТЕНИ: min_out считает КОТИРОВЩИК ТИПА (нуль
    чтений), а не цена события. У цены события наш объём не учтён, и выход она
    завышает; у котировщика цена из состояния ПОСЛЕ сделки источника -- то есть
    то самое число, с которым пошла бы боевая сборка. Поэтому тень здесь
    отвечает на вопрос «сколько бы дали», а не «сколько обещает чужая сделка».
    """
    C, _B, UN, TS, SB, _K = _moduli()
    iz = {"ok": False, "why_not": None, "route": ROUTE, "rezhim": rezhim(gruppa),
          "pool_program": None, "label": None, "way": None,
          "quote_mint": mint_kotirovki, "leg1_pool_program": None,
          "leg1_template_age_s": None, "leg1_min_out": None,
          "leg2_amount_in": None, "leg2_to_pool": None, "quote_fee_bps": None,
          "min_out": None, "expected_out": None, "min_out_from": None,
          "razvernut": None, "chtenij": 0, "kotirovshchik": None,
          # ЦЕНА СОБЫТИЯ ИСТОЧНИКА -- ТЕМИ ЖЕ ИМЕНАМИ, ЧТО У c2_usdc_noga.ten.
          # Числа не для отправки (наш объём в них не входит), а для журнала:
          # по ним видно, по какой цене прошёл САМ источник.
          "usdc_v_pul_istochnika": None, "token_iz_pula_istochnika": None}
    if rezhim(gruppa) == UN.MODE_OFF:
        iz["why_not"] = UN.WHY_OFF
        return iz
    try:
        pul = C.identify_pool(tx_istochnika, istochnik, mint)
        if not pul.get("ok"):
            iz["why_not"] = f"пул источника: {pul.get('why_not')}"
            return iz
        prog = UN.programma_pula(tx_istochnika, pul["pool_vault"])
        iz["pool_program"] = prog
        st = storona(tx_istochnika, programma=prog, hranilishche=pul["pool_vault"],
                     mint_kotirovki=mint_kotirovki)
        iz.update(label=st.get("label"), way=st.get("way"),
                  razvernut=st.get("razvernut"),
                  kotirovshchik=st.get("kotirovshchik"))
        if not st.get("ok"):
            iz["why_not"] = st.get("why_not")
            return iz
        if kesh_nog is None:
            iz["why_not"] = ("кэша шаблонов первой ноги нет -- билет в котировку "
                             "не посчитать")
            return iz
        e, vozrast = kesh_nog.get(mint_kotirovki)
        if e is None:
            iz["why_not"] = f"шаблона SOL -> {mint_kotirovki[:8]} в кэше нет"
            return iz
        iz["leg1_pool_program"] = e.get("program")
        iz["leg1_template_age_s"] = (round(vozrast, 1) if vozrast is not None
                                     else None)
        s = summy_nog(lamporty=lamporty, price_sol=e.get("price_sol"),
                      q_dec=e.get("q_dec"),
                      quote_program=(e.get("mv") or {}).get("base_program"),
                      nalog_bps=nalog_kotirovki_bps)
        iz.update(quote_fee_bps=s.get("quote_fee_bps"),
                  leg1_min_out=s.get("leg1_min_out"),
                  leg2_amount_in=s.get("leg2_amount_in"),
                  leg2_to_pool=s.get("leg2_to_pool"))
        if not s.get("ok"):
            iz["why_not"] = s.get("why_not")
            return iz
        try:
            c = UN.cena_sobytiya(tx_istochnika, st["tpl"],
                                 base_mint=st["base_mint"],
                                 quote_mint=mint_kotirovki)
            iz.update(usdc_v_pul_istochnika=c.get("usdc_v_pul"),
                      token_iz_pula_istochnika=c.get("token_iz_pula"))
        except Exception as exc:  # noqa: BLE001
            iz["cena_sobytiya_why_not"] = f"{type(exc).__name__}"
        kb = min_out_boj(st, tx_istochnika, amount_in=int(s["leg2_to_pool"]),
                         proskalzyvanie=proskalzyvanie,
                         mint_kotirovki=mint_kotirovki)
        iz.update(min_out=kb.get("min_out"), expected_out=kb.get("expected_out"),
                  min_out_from=kb.get("put"), fee_share=kb.get("fee_share"))
        if not kb.get("ok"):
            iz["why_not"] = kb.get("why_not")
            return iz
        if vozrast is not None and vozrast > SB.LEG_MAX_AGE_S:
            iz["why_not"] = (f"шаблон первой ноги старше {SB.LEG_MAX_AGE_S} с "
                             f"({vozrast:.0f} с)")
            return iz
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"тень USDC-ноги сигнальных типов: {UN._sled(exc)}"
        return iz
    iz["ok"] = True
    return iz


# ------------------------------------------------- список инструкций и сборка

def instrukcii(*, tx_istochnika: dict, istochnik: str, mint: str, nash_koshelek: str,
               lamporty: int, kesh_nog, proskalzyvanie: float = 0.35,
               cu_units: int | None = None, prioritet_lamporty: int = 1_000_000,
               chaevye_lamporty: int = 1_000_000, chaevye_spiskom: list | None = None,
               chaevye_adres: str | None = None, nons: tuple | None = None,
               nalog_kotirovki_bps=None, min_out_vneshnij: int | None = None,
               gruppa: str | None = None, kotirovka: str = "boj",
               mint_kotirovki: str = USDC) -> dict:
    """СПИСОК инструкций двух ног в порядке двухшагового пути -- без компиляции.

    Порядок -- РОВНО тот же, что у c2_usdc_noga.instrukcii (самопроверка сверяет
    его позиция в позицию):
        advance_nonce?, cu_limit, cu_price, ata(WSOL), ata(котировка), ata(база),
        sol_transfer, sync_native, своп ноги 1, своп ноги 2, чаевые
    """
    C, B, UN, TS, SB, _K = _moduli()
    if cu_units is None:
        cu_units = cu_nogi()
    iz = {"ok": False, "why_not": None, "route": ROUTE, "steps": 2,
          "rezhim": rezhim(gruppa), "pool_program": None, "label": None,
          "way": None, "quote_mint": mint_kotirovki, "leg1_pool_program": None,
          "leg1_template_age_s": None, "leg1_min_out": None, "leg2_amount_in": None,
          "leg2_to_pool": None, "quote_fee_bps": None, "min_out": None,
          "expected_out": None, "min_out_from": None, "razvernut": None,
          "slippage_used": proskalzyvanie, "ixs": None, "leg1_entry": None,
          "nonce_para": None, "tip_account": None, "tips": None,
          "tips_total_lamports": None, "kotirovshchik": None}
    if kesh_nog is None:
        iz["why_not"] = "кэша шаблонов первой ноги нет -- собирать не из чего"
        return iz
    np_ = TS.пара_нонса(nons)
    if not np_["ok"]:
        iz["why_not"] = np_["почему"]
        return iz
    nons = np_["пара"]
    iz["nonce_para"] = nons
    try:
        pul = C.identify_pool(tx_istochnika, istochnik, mint)
        if not pul.get("ok"):
            iz["why_not"] = f"пул источника: {pul.get('why_not')}"
            return iz
        prog = UN.programma_pula(tx_istochnika, pul["pool_vault"])
        iz["pool_program"] = prog
        st = storona(tx_istochnika, programma=prog, hranilishche=pul["pool_vault"],
                     mint_kotirovki=mint_kotirovki)
        iz.update(label=st.get("label"), way=st.get("way"),
                  razvernut=st.get("razvernut"), kotirovshchik=st.get("kotirovshchik"))
        if not st.get("ok"):
            iz["why_not"] = st.get("why_not")
            return iz
        e, vozrast = kesh_nog.get(mint_kotirovki)
        if e is None:
            iz["why_not"] = f"шаблона SOL -> {mint_kotirovki[:8]} в кэше нет"
            return iz
        iz["leg1_pool_program"] = e.get("program")
        iz["leg1_template_age_s"] = round(vozrast, 1) if vozrast is not None else None
        if vozrast is not None and vozrast > SB.LEG_MAX_AGE_S:
            iz["why_not"] = (f"шаблон первой ноги старше {SB.LEG_MAX_AGE_S} с "
                             f"({vozrast:.0f} с)")
            return iz
        mv1 = e.get("mv") or {}
        s = summy_nog(lamporty=lamporty, price_sol=e.get("price_sol"),
                      q_dec=e.get("q_dec"), quote_program=mv1.get("base_program"),
                      nalog_bps=nalog_kotirovki_bps)
        iz.update(quote_fee_bps=s.get("quote_fee_bps"),
                  leg1_min_out=s.get("leg1_min_out"),
                  leg2_amount_in=s.get("leg2_amount_in"),
                  leg2_to_pool=s.get("leg2_to_pool"))
        if not s.get("ok"):
            iz["why_not"] = s.get("why_not")
            return iz
        if isinstance(min_out_vneshnij, int) and min_out_vneshnij > 0:
            min_out = int(min_out_vneshnij)
            iz.update(min_out=min_out, min_out_from="передан полосой")
        else:
            kb = min_out_boj(st, tx_istochnika, amount_in=int(s["leg2_to_pool"]),
                             proskalzyvanie=proskalzyvanie,
                             mint_kotirovki=mint_kotirovki)
            iz.update(min_out=kb.get("min_out"), expected_out=kb.get("expected_out"),
                      min_out_from=kb.get("put"), chtenij=kb.get("chtenij"),
                      fee_share=kb.get("fee_share"))
            if not kb.get("ok"):
                iz["why_not"] = kb.get("why_not")
                return iz
            min_out = int(kb["min_out"])
        wsol_schet = B.ata(nash_koshelek, C.WSOL, mv1.get("quote_program"))
        ixs = []
        if nons:
            ixs.append(B.advance_nonce(str(nons[0]), str(nons[1])))
        ixs += [B.cu_limit(cu_units),
                B.cu_price(TS._цена_cu(prioritet_lamporty, cu_units)),
                B.ata_idempotent(nash_koshelek, nash_koshelek, C.WSOL,
                                 mv1.get("quote_program")),
                B.ata_idempotent(nash_koshelek, nash_koshelek, mint_kotirovki,
                                 mv1.get("base_program")),
                B.ata_idempotent(nash_koshelek, nash_koshelek, st["base_mint"],
                                 st["base_program"]),
                B.sol_transfer(nash_koshelek, wsol_schet, lamporty),
                B.sync_native(wsol_schet),
                B.swap_instruction(e["tpl"], e["tx"], nash_koshelek, lamporty,
                                   int(s["leg1_min_out"])),
                instrukciya_nogi(st, user=nash_koshelek,
                                 amount_in=int(s["leg2_amount_in"]),
                                 min_out=min_out, tx_istochnika=tx_istochnika)]
        pary = TS._пары_чаевых(chaevye_spiskom, chaevye_adres, chaevye_lamporty)
        for adres, lamp in pary:
            ixs.append(B.sol_transfer(nash_koshelek, adres, int(lamp)))
        if pary:
            iz["tip_account"] = pary[0][0]
            iz["tips"] = list(pary)
            iz["tips_total_lamports"] = sum(int(l_) for _, l_ in pary)
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"сборка USDC-ноги сигнальных типов: {UN._sled(exc)}"
        return iz
    iz.update(ok=True, ixs=ixs, leg1_entry=e)
    return iz


def sobrat(*, tx_istochnika: dict, istochnik: str, mint: str, nash_koshelek: str,
           lamporty: int, kesh_nog, proskalzyvanie: float = 0.35,
           cu_units: int | None = None, prioritet_lamporty: int = 1_000_000,
           chaevye_lamporty: int = 1_000_000, chaevye_spiskom: list | None = None,
           chaevye_adres: str | None = None, nons: tuple | None = None,
           rpc_call=None, gruppa: str | None = None, nalog_kotirovki_bps=None,
           min_out_vneshnij: int | None = None, nashi_tablicy: list | None = None,
           mint_kotirovki: str = USDC, luts_gotovye: list | None = None) -> dict:
    """ОДНА транзакция: SOL -> USDC -> токен на сигнальном типе. Без подписи.

    luts_gotovye -- таблицы адресов объектами (замер без сети); на денежном пути
    их не передают, там таблицы приходят из кэша ног, как у c2_usdc_noga.
    """
    t0 = time.perf_counter()
    _C, _B, UN, TS, SB, _K = _moduli()
    if rezhim(gruppa) != UN.MODE_LIVE:
        return {"ok": False, "route": ROUTE, "rezhim": rezhim(gruppa),
                "why_not": (UN.WHY_OFF if rezhim(gruppa) == UN.MODE_OFF
                            else UN.WHY_SHADOW),
                "tx_base64": None, "size": None}
    iz = instrukcii(
        tx_istochnika=tx_istochnika, istochnik=istochnik, mint=mint,
        nash_koshelek=nash_koshelek, lamporty=lamporty, kesh_nog=kesh_nog,
        proskalzyvanie=proskalzyvanie, cu_units=cu_units,
        prioritet_lamporty=prioritet_lamporty, chaevye_lamporty=chaevye_lamporty,
        chaevye_spiskom=chaevye_spiskom, chaevye_adres=chaevye_adres, nons=nons,
        nalog_kotirovki_bps=nalog_kotirovki_bps, min_out_vneshnij=min_out_vneshnij,
        gruppa=gruppa, mint_kotirovki=mint_kotirovki)
    ixs = iz.pop("ixs", None)
    e = iz.pop("leg1_entry", None)
    nons = iz.pop("nonce_para", None)
    iz.update(tx_base64=None, size=None, build_ms=None,
              nonce_account=(nons[0] if nons else None))
    if not iz.get("ok") or not ixs:
        iz["ok"] = False
        return iz
    try:
        import base64  # noqa: PLC0415

        from solders.hash import Hash  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.pubkey import Pubkey  # noqa: PLC0415
        from solders.signature import Signature  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415

        svoi = [k for k in (nashi_tablicy or []) if k]
        nuzhny = [k for k in svoi + SB._lut_keys(e["tx"])
                  + SB._lut_keys(tx_istochnika) if k not in kesh_nog.luts]
        if nuzhny and rpc_call is not None:
            kesh_nog.load_luts(nuzhny, rpc=rpc_call)
            iz["hot_lut_calls"] = 1
        klyuchi = list(dict.fromkeys(svoi + SB._lut_keys(e["tx"])
                                     + SB._lut_keys(tx_istochnika)))
        tablicy = [kesh_nog.luts[k] for k in klyuchi if k in kesh_nog.luts]
        tablicy += list(luts_gotovye or [])
        iz["lut_tables"] = len(tablicy)
        iz["nashi_tablicy_vzjaty"] = [k for k in svoi if k in kesh_nog.luts]
        iz["nashi_tablicy_ne_vzjaty"] = [k for k in svoi if k not in kesh_nog.luts]
        msg = MessageV0.try_compile(Pubkey.from_string(nash_koshelek), ixs, tablicy,
                                    Hash.default())
        syroe = bytes(VersionedTransaction.populate(
            msg, [Signature.default()] * msg.header.num_required_signatures))
    except Exception as exc:  # noqa: BLE001
        iz["ok"] = False
        iz["why_not"] = f"сборка USDC-ноги сигнальных типов: {UN._sled(exc)}"
        return iz
    iz["size"] = len(syroe)
    if len(syroe) > TS.ПРЕДЕЛ_РАЗМЕРА_TX:
        iz["ok"] = False
        iz["why_not"] = (f"транзакция {len(syroe)} байт при пределе сети "
                         f"{TS.ПРЕДЕЛ_РАЗМЕРА_TX}")
        iz["too_big"] = True
        return iz
    iz.update(ok=True, tx_base64=base64.b64encode(syroe).decode(),
              build_ms=round((time.perf_counter() - t0) * 1000, 3))
    return iz


# ------------------------------------- продажа: токен -> USDC -> SOL (вторая нога)

# ПРОДАЖА ПЕРВОЙ НОГИ -- ЗЕРКАЛО НАШЕЙ ЖЕ ПОКУПКИ ЭТОЙ НОГИ. Пул SOL -> USDC у
# полосы один (data/nogi_shablony.json, Pump AMM, глубина 21 851 SOL), и продажа
# USDC -> SOL на нём -- это та же покупка без двух мест, 19 и 20
# (user_volume_accumulator и его связанный счёт -- они только у покупки). Замер
# полосы (bloom_lane_sell, раскладка снята с НАШЕЙ собственной продажи): из
# 26-счётной покупки выходит 24 счёта. У нашего пула покупка 25-счётная, и те же
# два места дают 23 -- число, которое c2_swap_build.SPECS_ПРОДАЖИ допускает
# (min_accounts 23), но живой 23-счётной продажи Pump AMM в образцах
# репозитория НЕТ НИ ОДНОЙ. Поэтому шаблон несёт это полем
# zhivym_obrazcom_ne_provereno, а 6+ живых продаж Pump AMM с 23 счетами
# запрошены у Code-2 вместе с USDC-образцами.
PUMP_AMM_MESTA_TOLKO_POKUPKI = (19, 20)
PUMP_AMM_SCHETOV_PRODAZHI = (23, 24)
WHY_NOGA2_SCHETOV = ("продажа первой ноги: после выбрасывания мест 19 и 20 счетов "
                     "не 23 и не 24 -- раскладка не та")
WHY_NOGA2_NET_MIN = ("минимум выхода второй ноги продажи не передан: живых резервов "
                     "пула SOL/USDC этот модуль не читает (сеть -- не его дело)")


def noga_2_prodazhi(zapis_nogi_1: dict) -> dict:
    """Шаблон продажи USDC -> SOL: наша покупка первой ноги без мест 19 и 20."""
    _C, B, _UN, _TS, _SB, _K = _moduli()
    iz = {"ok": False, "why_not": None, "program": None, "accounts": None,
          "writable": None, "schetov_pokupki": None,
          "zhivym_obrazcom_ne_provereno": None}
    tpl = (zapis_nogi_1 or {}).get("tpl") or {}
    if not tpl.get("accounts"):
        iz["why_not"] = "шаблона первой ноги нет -- продажу её зеркалить не из чего"
        return iz
    sch = list(tpl["accounts"])
    iz["program"], iz["schetov_pokupki"] = tpl.get("program"), len(sch)
    if tpl.get("program") != PROG_PUMP_AMM:
        iz["why_not"] = (f"пул первой ноги {str(tpl.get('program'))[:8]}, а зеркало "
                         f"продажи измерено только у Pump AMM")
        return iz
    if len(sch) <= max(PUMP_AMM_MESTA_TOLKO_POKUPKI):
        iz["why_not"] = (f"в покупке ноги {len(sch)} счетов -- места "
                         f"{PUMP_AMM_MESTA_TOLKO_POKUPKI} из неё не выбросить")
        return iz
    prodazha = [a for i, a in enumerate(sch)
                if i not in PUMP_AMM_MESTA_TOLKO_POKUPKI]
    if len(prodazha) not in PUMP_AMM_SCHETOV_PRODAZHI:
        iz["why_not"] = f"{WHY_NOGA2_SCHETOV}: вышло {len(prodazha)}"
        return iz
    iz.update(ok=True, accounts=prodazha, writable=tpl.get("writable") or {},
              zhivym_obrazcom_ne_provereno=(len(prodazha) == 23))
    return iz


def instrukciya_nogi_2_prodazhi(shab: dict, *, nash_koshelek: str, kotirovki_v: int,
                                min_out: int):
    """sell(base_amount_in, min_quote_amount_out) на пуле первой ноги: вход USDC."""
    _C, B, _UN, _TS, _SB, _K = _moduli()
    if not (shab or {}).get("ok"):
        raise OshibkaNogi((shab or {}).get("why_not") or "шаблона продажи ноги нет")
    from solders.instruction import AccountMeta, Instruction  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    metas = [AccountMeta(Pubkey.from_string(a), is_signer=(a == nash_koshelek),
                         is_writable=bool(shab["writable"].get(a, False)))
             for a in shab["accounts"]]
    data = B.disc("sell") + struct.pack("<QQ", int(kotirovki_v), int(min_out))
    return Instruction(Pubkey.from_string(shab["program"]), data, metas)


# МЕСТА ПРОДАЖИ КРИВОЙ v2 -- ВСЕ НАШИ, А НЕ ТЕ, ЧТО ПРИШЛИ С ШАБЛОНОМ. Карта
# sell_v2 из IDL (её же проверяет c3_prodavec_sborka.места_кривая_v2): 13 user,
# 14 ATA базы, 15 ATA котировки, 19 накопитель объёма, 20 его ATA, 6/7 получатель
# комиссии и его ATA, 8/9 buyback и его ATA. ВСЕ ОНИ writable по IDL.
#
# ЗАЧЕМ ПОДСТАВЛЯТЬ, ЕСЛИ ШАБЛОН И ТАК ИЗ НАШЕЙ ПОКУПКИ. Потому что «и так» --
# это договорённость, а не проверка: шаблон приходит аргументом, и если в него
# попадёт ЧУЖАЯ покупка (своя же покупка не найдена, перепутан аргумент), то
# инструкция продажи уйдёт с ЧУЖИМИ счетами -- то есть попытается продать чужой
# остаток чужим кошельком. Подстановка делает это невозможным, а заодно ставит
# НАШИХ получателей комиссий (слово владельца 03.10: всегда один свой из Global).
KRIVAYA_V2_MESTA_PRODAZHI = {"user": 13, "ata_bazy": 14, "ata_kotirovki": 15,
                             "uva": 19, "assoc_uva": 20, "poluchatel": 6,
                             "ata_poluchatelya": 7, "buyback": 8,
                             "ata_buyback": 9, "mint_bazy": 1,
                             "mint_kotirovki": 2, "prog_bazy": 3,
                             "prog_kotirovki": 4}


def nabor_adresov_dlya_tablicy(scheta_pokupki_v2: list, *, nash_koshelek: str,
                               fajl_tablicy: str | None = None) -> dict:
    """НАБОР ПОСТОЯННЫХ АДРЕСОВ КРИВОЙ v2 для таблицы полосы -- точным списком.

    Что сюда входит и почему именно это постоянно:
      * НАШИ получатели комиссий (обычный и buyback) -- по слову владельца они
        всегда одни, и программа принимает любого из своих списков;
      * их ATA для WSOL и для USDC -- места 7 и 9 выводятся семенами IDL
        [получатель, программа котировки, минт котировки], то есть для каждой
        котировки свой, но для НАШЕГО получателя -- постоянный;
      * получатель для mayhem-монет (из reserved) и его два ATA -- отдельно: без
        mayhem они не нужны вовсе;
      * постоянные счета программы: global, global_volume_accumulator,
        fee_config, event_authority -- одни и те же у ЛЮБОГО пула кривой;
      * НАШ накопитель объёма и его ATA для USDC -- постоянны для кошелька;
      * программы (pfee, системная, сама кривая) -- для полноты списка; они в
        таблице уже лежат.
    Чего здесь НЕТ: счётов пула (кривая, её хранилища, минт базы) и счетов
    создателя -- они у каждой монеты свои, и в постоянной таблице им места нет.

    Состояние каждого адреса ("уже в таблице" / "добавить") считается по
    data/usdc_noga_alt.json -- то есть по той таблице, что создана и сверена по
    цепи, а не по памяти.
    """
    _C, B, _UN, _TS, _SB, _K = _moduli()
    iz = {"ok": False, "why_not": None, "nabor": None, "svod": None,
          "postojannye": None, "ata_usdc_poluchatelej": None,
          "nash_nakopitel": None, "mayhem": None}
    if len(scheta_pokupki_v2) != KRIVAYA_V2_SCHETOV:
        iz["why_not"] = (f"счетов {len(scheta_pokupki_v2)}, а у "
                         f"buy_exact_quote_in_v2 их {KRIVAYA_V2_SCHETOV}")
        return iz
    т = tablica_polosy(fajl_tablicy)
    if not т["ok"]:
        iz["why_not"] = т["why_not"]
        return iz
    в_таблице = set(т["adresa"])
    # ПРОГРАММА ТОКЕНА У WSOL И USDC -- КЛАССИЧЕСКАЯ SPL Token. Это не догадка:
    # у WSOL она такая по природе (её же берёт нога SOL -> USDC, см. шаблон
    # data/nogi_shablony.json), у USDC -- измерена на живой USDC-сделке Pump AMM
    # (место программы котировки = Tokenkeg...). Token-2022 бывает у ИНЫХ
    # котировок, и для них ATA получателя будет другим -- поэтому в наборе
    # названы ровно две котировки, а не «все».
    ТОКЕН = B.TOKEN_PROGRAM
    uva = B.pda([SEMYA_UVA, "USER"], nash_koshelek, PROG_KRIVAYA)
    ряды = []

    def _добавить(адрес, что):
        ряды.append({"адрес": адрес, "что": что,
                     "состояние": ("уже в таблице" if адрес in в_таблице
                                   else "добавить")})

    _добавить(POLUCHATEL_NASH, "получатель комиссии (обычный)")
    _добавить(BUYBACK_NASH, "buyback-получатель")
    _добавить(B.ata(POLUCHATEL_NASH, WSOL, ТОКЕН), "ATA получателя для WSOL")
    _добавить(B.ata(POLUCHATEL_NASH, USDC, ТОКЕН), "ATA получателя для USDC")
    _добавить(B.ata(BUYBACK_NASH, WSOL, ТОКЕН), "ATA buyback для WSOL")
    _добавить(B.ata(BUYBACK_NASH, USDC, ТОКЕН), "ATA buyback для USDC")
    _добавить(uva, "наш накопитель объёма (PDA)")
    _добавить(B.ata(uva, USDC, ТОКЕН), "ATA накопителя для USDC")
    for место, имя in ((0, "global"), (19, "global_volume_accumulator"),
                       (22, "fee_config"), (25, "event_authority"),
                       (23, "программа комиссий (fee_program)"),
                       (24, "системная программа"), (26, "программа кривой")):
        _добавить(scheta_pokupki_v2[место], f"место {место}: {имя}")
    майхем = []
    for адрес, что in ((POLUCHATEL_NASH_MAYHEM, "получатель для mayhem (reserved)"),
                       (B.ata(POLUCHATEL_NASH_MAYHEM, WSOL, ТОКЕН),
                        "ATA mayhem-получателя для WSOL"),
                       (B.ata(POLUCHATEL_NASH_MAYHEM, USDC, ТОКЕН),
                        "ATA mayhem-получателя для USDC")):
        _добавить(адрес, что)
        if адрес not in в_таблице:
            майхем.append(адрес)
    добавить = [р["адрес"] for р in ряды if р["состояние"] == "добавить"]
    iz.update(ok=True, nabor=ряды, mayhem=майхем,
              postojannye=[scheta_pokupki_v2[i] for i in KRIVAYA_MESTA_POSTOYANNYH],
              ata_usdc_poluchatelej=[B.ata(POLUCHATEL_NASH, USDC, ТОКЕН),
                                     B.ata(BUYBACK_NASH, USDC, ТОКЕН)],
              nash_nakopitel=[uva, B.ata(uva, USDC, ТОКЕН)],
              svod={"vsego": len(ряды),
                    "uzhe_v_tablice": sum(1 for р in ряды
                                          if р["состояние"] == "уже в таблице"),
                    "dobavit": len(добавить),
                    "dobavit_bez_mayhem": len([а for а in добавить
                                               if а not in майхем]),
                    "adresov_v_tablice_stanet": len(в_таблице | set(добавить))})
    return iz


def nashi_mesta_prodazhi_v2(shablon: dict, *, nash_koshelek: str,
                            mayhem: bool = False) -> dict:
    """Шаблон продажи кривой v2 с НАШИМИ местами и НАШИМИ получателями."""
    _C, B, _UN, _TS, _SB, _K = _moduli()
    M = KRIVAYA_V2_MESTA_PRODAZHI
    iz = {"ok": False, "why_not": None, "shablon": None, "podstavleno": None}
    сч = list((shablon or {}).get("accounts") or [])
    if len(сч) != 26:
        iz["why_not"] = f"счетов продажи {len(сч)}, а у sell_v2 их 26"
        return iz
    мб, мк = сч[M["mint_bazy"]], сч[M["mint_kotirovki"]]
    пб, пк = сч[M["prog_bazy"]], сч[M["prog_kotirovki"]]
    пол = POLUCHATEL_NASH_MAYHEM if mayhem else POLUCHATEL_NASH
    список = POLUCHATELI_MAYHEM if mayhem else POLUCHATELI_OBYCHNYE
    if пол not in список or BUYBACK_NASH not in POLUCHATELI_BUYBACK:
        iz["why_not"] = f"{WHY_POLUCHATEL_NE_V_SPISKE}: {пол[:8]}"
        return iz
    uva = B.pda([SEMYA_UVA, "USER"], nash_koshelek, PROG_KRIVAYA)
    наши = {M["user"]: nash_koshelek,
            M["ata_bazy"]: B.ata(nash_koshelek, мб, пб),
            M["ata_kotirovki"]: B.ata(nash_koshelek, мк, пк),
            M["uva"]: uva,
            M["assoc_uva"]: B.ata(uva, мк, пк),
            M["poluchatel"]: пол,
            M["ata_poluchatelya"]: B.ata(пол, мк, пк),
            M["buyback"]: BUYBACK_NASH,
            M["ata_buyback"]: B.ata(BUYBACK_NASH, мк, пк)}
    новые = list(сч)
    права = dict(shablon.get("writable") or {})
    for место, адрес in наши.items():
        новые[место] = адрес
        # ПРИЗНАК ЗАПИСИ -- ПО IDL, А НЕ ПО КАРТЕ АДРЕСОВ. Карта в шаблоне
        # ключуется АДРЕСОМ: подставив свой, мы потеряли бы её значение и
        # получили writable=False на счёте, который программа пишет.
        права[адрес] = True
    iz.update(ok=True, podstavleno=наши,
              shablon=dict(shablon, accounts=новые, writable=права))
    return iz


def prodazha_instrukcii(*, tx_pokupki: dict, programma: str, nash_koshelek: str,
                        ostatok: int, kesh_nog, mint_bazy: str | None = None,
                        hranilishche: str | None = None,
                        mint_kotirovki: str = USDC,
                        min_out_nogi_1: int | None = None,
                        min_out_nogi_2: int | None = None,
                        proskalzyvanie: float = 0.35,
                        cu_units: int | None = None,
                        prioritet_lamporty: int = 1_000_000,
                        chaevye_lamporty: int = 0,
                        chaevye_spiskom: list | None = None,
                        chaevye_adres: str | None = None,
                        nons: tuple | None = None,
                        zakryvat_schet_tokena: bool | None = None,
                        ostatok_usdc: int | None = None) -> dict:
    """СПИСОК инструкций продажи токен -> USDC -> SOL. Без компиляции и подписи.

    Первая нога -- зеркало НАШЕЙ покупки токена (c3_prodavec_sborka: шаблон из
    покупки, инструкция типа); вторая -- зеркало НАШЕЙ покупки первой ноги
    (Pump AMM sell на пуле SOL/USDC). Минимумы: первой ноги -- котировщик
    продажи (c3_kotirovka_prodazhi, цена в единицах USDC), второй -- ТОЛЬКО
    переданный: живые резервы пула SOL/USDC читает полоса, не этот модуль.

    Порядок -- зеркало покупки, место в место:
        advance_nonce?, cu_limit, cu_price, ata(USDC), ata(WSOL),
        продажа ноги 1 (токен -> USDC), продажа ноги 2 (USDC -> SOL),
        закрытие счёта токена, закрытие счёта WSOL, чаевые
    """
    C, B, UN, TS, _SB, K = _moduli()
    import c3_prodavec_sborka as S  # noqa: PLC0415
    if cu_units is None:
        cu_units = cu_nogi()
    iz = {"ok": False, "why_not": None, "route": ROUTE + "_prodazha", "steps": 2,
          "rezhim": rezhim(None), "pool_program": programma, "label": None,
          "quote_mint": mint_kotirovki, "min_out_nogi_1": None,
          "min_out_nogi_2": None, "noga_2_amount_in": None,
          "min_out_1_otkuda": None, "ixs": None, "nonce_para": None,
          "zakryt_schet_tokena": None, "zakryt_schet_wsol": None,
          "noga_2_zhivym_obrazcom_ne_proverena": None, "tips": None,
          "tip_account": None, "tips_total_lamports": None}
    if kesh_nog is None:
        iz["why_not"] = "кэша шаблонов первой ноги нет -- вторую ногу зеркалить не из чего"
        return iz
    np_ = TS.пара_нонса(nons)
    if not np_["ok"]:
        iz["why_not"] = np_["почему"]
        return iz
    nons = np_["пара"]
    iz["nonce_para"] = nons
    try:
        if not isinstance(ostatok, int) or ostatok <= 0:
            iz["why_not"] = f"остаток не положительное целое: {ostatok!r}"
            return iz
        sh1 = S.шаблон_продажи(tx_pokupki, программа=programma, минт_базы=mint_bazy,
                              минт_котировки=mint_kotirovki,
                              хранилище=hranilishche)
        iz["label"] = sh1.get("метка")
        if not sh1.get("ok"):
            iz["why_not"] = f"шаблон продажи первой ноги: {sh1.get('why_not')}"
            return iz
        iz["zhivaya_prodazha_s_etoj_kotirovkoj"] = sh1.get(
            "живая_продажа_с_этой_котировкой")
        if programma == PROG_KRIVAYA:
            нм = nashi_mesta_prodazhi_v2(sh1, nash_koshelek=nash_koshelek)
            iz["nashi_mesta_prodazhi"] = нм.get("podstavleno")
            if not нм["ok"]:
                iz["why_not"] = f"места продажи кривой: {нм['why_not']}"
                return iz
            sh1 = нм["shablon"]
        if isinstance(min_out_nogi_1, int) and min_out_nogi_1 > 0:
            mo1, iz["min_out_1_otkuda"] = int(min_out_nogi_1), "передан полосой"
        else:
            k1 = K.котировка_продажи(programma, пул=None, хранилище=hranilishche,
                                     остаток=int(ostatok), tx_покупки=tx_pokupki,
                                     минт_базы=sh1.get("минт_базы"),
                                     минт_котировки=mint_kotirovki,
                                     проскальзывание=proskalzyvanie)
            iz["min_out_1_otkuda"] = k1.get("путь")
            if not k1.get("ok") or not k1.get("min_out"):
                iz["why_not"] = f"цена продажи первой ноги: {k1.get('why_not')}"
                return iz
            # МИНТ ВЫХОДА НАЗВАН: на кривой с котировочным минтом выход в
            # единицах USDC, а не в лампортах, и спутать их нельзя.
            if k1.get("минт_котировки") not in (None, mint_kotirovki):
                iz["why_not"] = (f"котировщик продажи считал в "
                                 f"{str(k1.get('минт_котировки'))[:8]}, а нога "
                                 f"продаёт в {mint_kotirovki[:8]}")
                return iz
            mo1 = int(k1["min_out"])
        iz["min_out_nogi_1"] = mo1
        e, _vozrast = kesh_nog.get(mint_kotirovki)
        if e is None:
            iz["why_not"] = f"шаблона SOL -> {mint_kotirovki[:8]} в кэше нет"
            return iz
        sh2 = noga_2_prodazhi(e)
        iz["noga_2_zhivym_obrazcom_ne_proverena"] = sh2.get(
            "zhivym_obrazcom_ne_provereno")
        if not sh2.get("ok"):
            iz["why_not"] = f"шаблон продажи второй ноги: {sh2.get('why_not')}"
            return iz
        # ВХОД ВТОРОЙ НОГИ -- МИНИМУМ ПЕРВОЙ ПЛЮС УЖЕ ЛЕЖАЩИЙ НА СЧЁТЕ USDC
        # (добавка MRKL, слово владельца 03.10). Прежде входом был РОВНО
        # минимум первой ноги -- "больше, чем принесла первая, вторая потратить
        # не может", и это верно про саму продажу. Но у покупки первая нога
        # берёт SOL -> USDC с проскальзыванием и отдаёт в пул токена ровно свой
        # min_out: разница между полученным USDC и этим минимумом остаётся на
        # нашем счёте USDC и не уходит НИКОГДА -- копится монетой, которой в
        # учёте полосы нет вовсе. Теперь продажа может забрать и её.
        # ЧИСЛО ПРИХОДИТ СНАРУЖИ: остаток живёт на цепи, а этот модуль сети не
        # читает вовсе. Не передали -- ведём себя как прежде и говорим это полем,
        # а не молча.
        _нога2_вход = int(mo1) + int(ostatok_usdc or 0)
        iz["noga_2_ostatok_usdc"] = (None if ostatok_usdc is None
                                     else int(ostatok_usdc))
        iz["noga_2_ostatok_why_not"] = (
            None if ostatok_usdc is not None
            else "остаток USDC не передан -- вторая нога идёт только минимумом "
                 "первой, остаток останется на счёте")
        iz["noga_2_amount_in"] = int(_нога2_вход)
        if not (isinstance(min_out_nogi_2, int) and min_out_nogi_2 > 0):
            iz["why_not"] = WHY_NOGA2_NET_MIN
            return iz
        iz["min_out_nogi_2"] = int(min_out_nogi_2)
        mv1 = e.get("mv") or {}
        schet_usdc = B.ata(nash_koshelek, mint_kotirovki, mv1.get("base_program"))
        schet_wsol = B.ata(nash_koshelek, C.WSOL, mv1.get("quote_program"))
        ixs = []
        if nons:
            ixs.append(B.advance_nonce(str(nons[0]), str(nons[1])))
        ixs += [B.cu_limit(cu_units),
                B.cu_price(TS._цена_cu(prioritet_lamporty, cu_units)),
                B.ata_idempotent(nash_koshelek, nash_koshelek, mint_kotirovki,
                                 mv1.get("base_program")),
                B.ata_idempotent(nash_koshelek, nash_koshelek, C.WSOL,
                                 mv1.get("quote_program")),
                S.инструкция_продажи(sh1, наш_кошелёк=nash_koshelek,
                                     база_в=int(ostatok), минимум_выхода=mo1),
                instrukciya_nogi_2_prodazhi(sh2, nash_koshelek=nash_koshelek,
                                            kotirovki_v=int(_нога2_вход),
                                            min_out=int(min_out_nogi_2))]
        # ЗАКРЫТИЕ СЧЕТОВ -- ТОЙ ЖЕ ТРАНЗАКЦИЕЙ, как у зеркальной продажи полосы:
        # токеновый счёт закрывается только когда продан весь остаток, а счёт
        # WSOL закрывается всегда -- иначе SOL остался бы завёрнутым.
        # УМОЛЧАНИЕ -- ПО ТИПУ. None значит "как решает тип" (п.1б владельца);
        # явный True или False по-прежнему сильнее таблицы: замер и круг должны
        # уметь мерить оба режима.
        _закрывать = (zakryvat_schet_tokena if zakryvat_schet_tokena is not None
                      else zakryvat_schet_tokena_po_tipu(programma))
        iz["zakryvaem_schet_tokena"] = bool(_закрывать)
        iz["zakryvaem_po_tipu"] = zakryvat_schet_tokena is None
        if _закрывать and sh1.get("минт_базы"):
            schet_bazy = B.ata(nash_koshelek, sh1["минт_базы"],
                               sh1.get("программа_базы") or B.TOKEN_PROGRAM)
            ixs.append(S.инструкция_закрытия(
                schet_bazy, nash_koshelek,
                sh1.get("программа_базы") or B.TOKEN_PROGRAM))
            iz["zakryt_schet_tokena"] = schet_bazy
        ixs.append(S.инструкция_закрытия(schet_wsol, nash_koshelek,
                                         mv1.get("quote_program")))
        iz["zakryt_schet_wsol"] = schet_wsol
        iz["schet_usdc"] = schet_usdc
        pary = TS._пары_чаевых(chaevye_spiskom, chaevye_adres, chaevye_lamporty)
        for adres, lamp in pary:
            ixs.append(B.sol_transfer(nash_koshelek, adres, int(lamp)))
        if pary:
            iz["tip_account"] = pary[0][0]
            iz["tips"] = list(pary)
            iz["tips_total_lamports"] = sum(int(l_) for _, l_ in pary)
    except Exception as exc:  # noqa: BLE001
        iz["why_not"] = f"сборка продажи USDC-ноги: {UN._sled(exc)}"
        return iz
    iz.update(ok=True, ixs=ixs, leg1_entry=e)
    return iz


def prodazha_sobrat(*, luts_gotovye: list | None = None,
                    nashi_tablicy: list | None = None, rpc_call=None,
                    **kw) -> dict:
    """ОДНА транзакция продажи токен -> USDC -> SOL. Без подписи и отправки."""
    t0 = time.perf_counter()
    _C, _B, UN, TS, SB, _K = _moduli()
    if rezhim(kw.get("gruppa")) != UN.MODE_LIVE:
        return {"ok": False, "route": ROUTE + "_prodazha",
                "rezhim": rezhim(kw.get("gruppa")),
                "why_not": (UN.WHY_OFF if rezhim(kw.get("gruppa")) == UN.MODE_OFF
                            else UN.WHY_SHADOW), "tx_base64": None, "size": None}
    kw.pop("gruppa", None)
    iz = prodazha_instrukcii(**kw)
    ixs = iz.pop("ixs", None)
    e = iz.pop("leg1_entry", None)
    iz.update(tx_base64=None, size=None, build_ms=None)
    if not iz.get("ok") or not ixs:
        iz["ok"] = False
        return iz
    try:
        import base64  # noqa: PLC0415

        from solders.hash import Hash  # noqa: PLC0415
        from solders.message import MessageV0  # noqa: PLC0415
        from solders.pubkey import Pubkey  # noqa: PLC0415
        from solders.signature import Signature  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415

        kesh_nog = kw["kesh_nog"]
        svoi = [k for k in (nashi_tablicy or []) if k]
        nuzhny = [k for k in svoi + SB._lut_keys(e["tx"])
                  + SB._lut_keys(kw.get("tx_pokupki") or {})
                  if k not in kesh_nog.luts]
        if nuzhny and rpc_call is not None:
            kesh_nog.load_luts(nuzhny, rpc=rpc_call)
            iz["hot_lut_calls"] = 1
        klyuchi = list(dict.fromkeys(svoi + SB._lut_keys(e["tx"])
                                     + SB._lut_keys(kw.get("tx_pokupki") or {})))
        tablicy = [kesh_nog.luts[k] for k in klyuchi if k in kesh_nog.luts]
        tablicy += list(luts_gotovye or [])
        iz["lut_tables"] = len(tablicy)
        msg = MessageV0.try_compile(Pubkey.from_string(kw["nash_koshelek"]), ixs,
                                    tablicy, Hash.default())
        syroe = bytes(VersionedTransaction.populate(
            msg, [Signature.default()] * msg.header.num_required_signatures))
    except Exception as exc:  # noqa: BLE001
        iz["ok"] = False
        iz["why_not"] = f"сборка продажи USDC-ноги: {UN._sled(exc)}"
        return iz
    iz["size"] = len(syroe)
    if len(syroe) > TS.ПРЕДЕЛ_РАЗМЕРА_TX:
        iz["ok"] = False
        iz["why_not"] = (f"транзакция продажи {len(syroe)} байт при пределе сети "
                         f"{TS.ПРЕДЕЛ_РАЗМЕРА_TX}")
        iz["too_big"] = True
        return iz
    iz.update(ok=True, tx_base64=base64.b64encode(syroe).decode(),
              build_ms=round((time.perf_counter() - t0) * 1000, 3))
    return iz


# ------------------------------------------------------------- самопроверка

# ЧИСЛО ПРОВЕРОК ОБЪЯВЛЕНО ЗАРАНЕЕ: молчаливый пропуск -- это провал. Если файла
# образцов нет или тип перестал разбираться, проверок станет МЕНЬШЕ, и
# самопроверка упадёт на несовпадении числа, а не промолчит зелёным.
# 202, А НЕ 186: Code-1 правил ЭТОТ файл от моей копии на 166 проверок (его
# двадцать) -- слияние сложило его двадцать и мои шестнадцать (получатели и
# сверка порядка резервов). Число проверок от содержимого таблицы НЕ зависит:
# сверено прогоном одного и того же кода на таблице из 34 и из 42 адресов --
# 202 и там и там, расходятся только ЧИСЛА в именах.
ZHDEM_PROVEROK = 202

# ЗАМЕР ПО ТИПАМ НА ЖИВЫХ СДЕЛКАХ С КОТИРОВОЧНЫМ ТОКЕНОМ (не WSOL). Образцы --
# data/c2_pool_samples/<программа>.json плюс разновидности кривой
# (data/c2_curve_variant_samples.json), их же читает c2_swap_build.load_samples.
# Котировка РОВНО USDC по этим четырём типам в образцах не встречается ни разу --
# запрошено у Code-2 по 6+ живых USDC-свопов на тип.
ZHDEM_PO_TIPAM = {
    # CPMM: цена отказывает на 13 образцах из 43 -- поле f > 1 (не снятые
    # protocol_fees/fund_fees в хранилище), отказ по имени, а не тихая подмена.
    PROG_CPMM: {"obrazcov": 43, "storona": 43, "bajt": 43, "cena": 30,
                "otkaz_ceny": 13},
    PROG_LAUNCHLAB: {"obrazcov": 35, "storona": 35, "bajt": 35, "cena": 35,
                     "otkaz_ceny": 0},
    # Pump AMM: живых сделок с котировочным токеном в образцах всего две (23 из
    # 25 -- с WSOL), и ОДНА из них -- с котировкой РОВНО USDC. Это мало, и так и
    # сказано числом.
    PROG_PUMP_AMM: {"obrazcov": 2, "storona": 2, "bajt": 2, "cena": 2,
                    "otkaz_ceny": 0},
    # Кривая: три живые покупки с котировочным минтом (HiMSSzzwkZ...,
    # 3NZ9JMVBmGAq..., XspzcW1PRtgf...). У одной цена отказывает по имени:
    # событие НЕ воспроизвело свою покупку (кривая дала 1 561 355 524 924,
    # в событии 1 561 186 921 389 -- расхождение 1.1e-4, а допуск 1e-6). Это
    # ровно та проверка, ради которой событие не берётся на слово.
    PROG_KRIVAYA: {"obrazcov": 3, "storona": 3, "bajt": 3, "cena": 2,
                   "otkaz_ceny": 1},
}
ZHDEM_BAJT_VSEGO = 83

# РАЗМЕР ПАКЕТА -- ЗАМЕР НА ЭТИХ ЖЕ ОБРАЗЦАХ, с нашей таблицей, В ЧЕТЫРЁХ
# РЕЖИМАХ -- тех, что полоса реально кладёт в транзакцию. Во всех режимах в
# таблицу добавлены РОВНО те три адреса, которые на USDC-котировке в ней уже
# есть: минт котировки, наш ATA котировки, программа токена котировки (у образца
# котировка своя, и это арифметика по собранному пакету, а не догадка).
#
# ПЕРВЫЙ ЗАМЕР БЫЛ НИЖНЕЙ ГРАНИЦЕЙ, И ЭТО НАШЛА ПРОВЕРКА (03.10): чаевые шли НА
# НАШ ЖЕ КОШЕЛЁК, а такой перевод нового счёта в пакет не добавляет вовсе.
# Настоящие случаи:
#   chaevyj_nash    -- чаевые на наш кошелёк: нижняя граница, в бою не бывает;
#   chaevyj_chuzhoj -- адрес из bloom_own_send.TIP_ACCOUNTS: +32 байта (адресов
#                      чаевых в нашей таблице НЕТ -- lane_usdc_alt:
#                      «чаевых_не_положено: 31»);
#   nons            -- долговечный nonce без чаевых (в режиме нонса полоса
#                      чаевые основной транзакции снимает вовсе): +2 счёта,
#                      сам счёт нонса и Sysvar recent blockhashes;
#   nons_i_chaevyj  -- и то и другое: САМЫЙ ТЯЖЁЛЫЙ, по нему и решать.
# Разница между крайними режимами -- ровно 106 байт на каждом типе.
TIP_DLYA_ZAMERA = "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE"
NONS_DLYA_ZAMERA = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
REZHIMY_PAKETA = ("chaevyj_nash", "chaevyj_chuzhoj", "nons", "nons_i_chaevyj")
ZHDEM_RAZMEROV = {
    PROG_CPMM: {"chaevyj_nash": (915, 947), "chaevyj_chuzhoj": (947, 979),
                "nons": (972, 1004), "nons_i_chaevyj": (1021, 1053)},
    PROG_LAUNCHLAB: {"chaevyj_nash": (1024, 1056), "chaevyj_chuzhoj": (1056, 1088),
                     "nons": (1081, 1113), "nons_i_chaevyj": (1130, 1162)},
    PROG_PUMP_AMM: {"chaevyj_nash": (1024, 1056), "chaevyj_chuzhoj": (1056, 1088),
                    "nons": (1081, 1113), "nons_i_chaevyj": (1130, 1162)},
    # КРИВАЯ -- ПОСЛЕ ФИКСАЦИИ ПОЛУЧАТЕЛЕЙ И ПОСЛЕ ДОЛИВКИ ТАБЛИЦЫ. Два шага,
    # и оба замерены: (1) слово владельца 03.10 -- всегда наши получатели, и
    # пакет стал на 32...64 байта меньше, чем с получателями источника; (2)
    # Code-1 ДОЛИЛ в таблицу полосы восемь адресов живьём (подпись
    # 5nHwzF8yUsccTgX3mEPTobyjYaZHy23oDMaL4x32LzE8V3FT9XnVYbWy6bLVpadRV4uLjpKZrMK8wbVnFLmsppy7,
    # было 34 адреса -- стало 42). После этого КРИВАЯ ВЛЕЗАЕТ В БОЕВОМ РЕЖИМЕ:
    # 1168...1199 при пределе 1232, все три образца. До доливки не влезал ни один.
    PROG_KRIVAYA: {"chaevyj_nash": (1062, 1093), "chaevyj_chuzhoj": (1094, 1125),
                   "nons": (1119, 1150), "nons_i_chaevyj": (1168, 1199)},
}
# Сколько живых сделок каждого типа влезает в 1232 в САМОМ ТЯЖЁЛОМ режиме.
# КРИВАЯ -- 3 из 3 ПОСЛЕ ДОЛИВКИ ТАБЛИЦЫ (было 0 из 3 при таблице в 34 адреса).
ZHDEM_VLEZLO_TYAZHELYJ = {PROG_CPMM: 43, PROG_LAUNCHLAB: 35, PROG_PUMP_AMM: 2,
                          PROG_KRIVAYA: 3}
# ЛЕСТНИЦА ДОБАВОК -- ЗАНОВО ПО ЖИВОЙ ТАБЛИЦЕ (42 адреса). Четыре постоянные
# программы кривой Code-1 уже долил, поэтому «+4» теперь РАВНО «+0»: добавлять
# нечего, они в таблице. Остаток лестницы говорит, что ещё стоит долить:
#   +6 -- ATA наших двух получателей ДЛЯ USDC. На этом образце размер не меняется
#         (его котировка -- не USDC, и в транзакции стоят ATA для ЕГО минта), но
#         в НАСТОЯЩЕЙ сделке с котировкой USDC эти два адреса в транзакции есть,
#         и каждый из них -- 31 байт;
#   +8 -- наш накопитель объёма (PDA кошелька) и его ATA для USDC. PDA в
#         транзакции стоит ВСЕГДА, и он даёт замеренные 1199 -> 1168.
# То есть 1199 -- ВЕРХНЯЯ ГРАНИЦА: в сделке с котировкой USDC по долитой таблице
# размер будет не больше, а меньше.
#
# Прежний замер (таблица 34 адреса) для памяти: покупка 1323, продажа 1331,
# продажа без закрытия 1293 -- не влезало ничто.
KRIVAYA_DOLITO_PODPIS = ("5nHwzF8yUsccTgX3mEPTobyjYaZHy23oDMaL4x32LzE8V3FT9Xn"
                         "VYbWy6bLVpadRV4uLjpKZrMK8wbVnFLmsppy7")
# Считаются ТОЛЬКО адреса, которых в таблице полосы ещё нет:
#   4  -- постоянные программы кривой: global, global_volume_accumulator,
#         fee_config, event_authority (места 0, 19, 22, 25);
#   +2 -- ATA НАШИХ получателей для USDC (их ATA для WSOL уже в таблице:
#         те же получатели обслуживают пул первой ноги);
#   +2 -- НАШ накопитель объёма и его ATA для USDC (постоянны для кошелька).
# Восьми хватает и покупке, и продаже; на четырёх продажа влезает только без
# закрытия токенового счёта.
KRIVAYA_MESTA_POSTOYANNYH = (0, 19, 22, 25)
ZHDEM_KRIVAYA_LESTNICA = {
    0: {"pokupka": 1199, "prodazha": 1200, "prodazha_bez_zakrytiya": 1200},
    4: {"pokupka": 1199, "prodazha": 1200, "prodazha_bez_zakrytiya": 1200},
    6: {"pokupka": 1199, "prodazha": 1200, "prodazha_bez_zakrytiya": 1200},
    8: {"pokupka": 1168, "prodazha": 1169, "prodazha_bez_zakrytiya": 1169},
}
# НАБОР, КОТОРЫЙ УХОДИТ CODE-1 (восемь адресов плюс три на mayhem). Точные
# адреса выводятся числом в nabor_adresov_dlya_tablicy() -- в коде их нет,
# кроме получателей: они из публичных списков программы.
# ПОСЛЕ ДОЛИВКИ 03.10: из восемнадцати в таблице уже одиннадцать, долить
# осталось семь -- четыре без mayhem (ATA двух наших получателей для USDC, наш
# накопитель объёма и его ATA для USDC) и три на mayhem. Накопитель и его ATA
# выводятся из КОШЕЛЬКА, и в этом замере кошелёк проверочный: для боя их обязан
# посчитать Code-1 от боевого кошелька (функция nabor_adresov_dlya_tablicy
# принимает его параметром).
# ТО ЖЕ ПОСЛЕ ЖИВОГО РАСШИРЕНИЯ 17:24Z: из 18 адресов набора в таблице уже 16,
# доливать осталось 2. Числа Code-3 (11 / 7 / 4) мерили таблицу ДО расширения.
ZHDEM_NABORA = {"vsego": 18, "uzhe_v_tablice": 16, "dobavit": 2,
                "dobavit_bez_mayhem": 2, "adresov_v_tablice_stanet": 49}
# ПОЧЕМУ НЕ ДВУХШАГОВЫМ ПУТЁМ -- ЧИСЛОМ, А НЕ РАССУЖДЕНИЕМ. bloom_lane_two_step
# вторую ногу этих типов СОБИРАЕТ (кирпичи те же), но отправить её не может: у
# него нет НАШЕЙ таблицы адресов, и пакет выходит за 1232 байта на ВСЕХ живых
# сделках. Эти числа -- замер его же функцией собрать() на тех же образцах и с
# тем же шаблоном первой ноги. У кривой он отказывает раньше -- на цене: её
# котировщик (c2_swap_build.bonding_min_out) считает лампортами, а на токеновой
# котировке обязательная часть события пуста по SOL.
ZHDEM_DVUHSHAGOVYJ = {
    PROG_CPMM: {"razmer": (1453, 1485), "pochemu": "при пределе сети"},
    PROG_LAUNCHLAB: {"razmer": (1562, 1594), "pochemu": "при пределе сети"},
    PROG_PUMP_AMM: {"razmer": (1562, 1594), "pochemu": "при пределе сети"},
    PROG_KRIVAYA: {"razmer": None,
                   "pochemu": "минимум шага 2 не выдаётся"},
}
# ПРОДАЖА: РАЗМЕР ПАКЕТА токен -> USDC -> SOL, с нашей таблицей и с чаевыми.
# Восемь инструкций: cu_limit, cu_price, ata(USDC), ata(WSOL), продажа ноги 1,
# продажа ноги 2, закрытие счёта токена, закрытие счёта WSOL (+ чаевые).
ZHDEM_PRODAZHI = {
    PROG_CPMM: {"chaevyj_nash": (1021, 1052), "nons_i_chaevyj": (1127, 1158)},
    PROG_LAUNCHLAB: {"chaevyj_nash": (1130, 1161), "nons_i_chaevyj": (1236, 1267)},
    PROG_PUMP_AMM: {"chaevyj_nash": (1128, 1159), "nons_i_chaevyj": (1234, 1265)},
    # КРИВАЯ -- с НАШИМИ местами продажи, НАШИМИ получателями и по ДОЛИТОЙ
    # таблице. Было 1352...1384 и 1458...1490, когда места и получатели
    # приходили из ЧУЖОЙ покупки (то есть и продажа ушла бы чужими счетами), и
    # 1225/1331 при таблице в 34 адреса. С ЗАКРЫТИЕМ счёта токена 1238 всё ещё
    # НЕ влезает -- влезает умолчание по типу (без закрытия, 1200).
    PROG_KRIVAYA: {"chaevyj_nash": (1132, 1132), "nons_i_chaevyj": (1238, 1238)},
}
# ПРОДАЖА В РЕЖИМЕ НОНСА НЕ ВЛЕЗАЕТ У LAUNCHLAB И PUMP AMM -- перебор 4...35
# байт, -- и лечится тем же, чем у кривой: не закрывать токеновый счёт той же
# транзакцией (рента остаётся на счёте и забирается отдельно -- так полоса и
# делала до 02.10 прогоном bloom_close_on_sell). Замер в самом тяжёлом режиме
# БЕЗ закрытия: влезает всё, кроме кривой.
ZHDEM_PRODAZHI_BEZ_ZAKRYTIYA = {
    PROG_CPMM: (1088, 1119), PROG_LAUNCHLAB: (1197, 1228),
    PROG_PUMP_AMM: (1195, 1226), PROG_KRIVAYA: (1200, 1200),
}
ZHDEM_VLEZLO_PRODAZHA_TYAZHELYJ = {PROG_CPMM: 43, PROG_LAUNCHLAB: 0,
                                   PROG_PUMP_AMM: 0, PROG_KRIVAYA: 0}
# ПРОДАЖА КРИВОЙ СОБИРАЕТСЯ НА ДВУХ ОБРАЗЦАХ ИЗ ТРЁХ: у третьего цена продажи
# отказывает по имени -- событие его покупки не воспроизвело себя, и котировать
# продажу нечем.
ZHDEM_PRODAZH_KRIVOJ = 2
# ПРОДАЖА КРИВОЙ: ВЫБОР НЕ МОЙ, И ОБА ЧИСЛА ЗАМЕРЕНЫ. В лёгком режиме (чаевые
# на наш кошелёк) хватало четырёх адресов без закрытия счёта; в САМОМ ТЯЖЁЛОМ
# (нонс + чужой чаевый) влезает только «десять адресов И без закрытия» -- 1202.
ZHDEM_PRODAZHA_KRIVOJ = {"4_zakryvaem": 1259, "10_zakryvaem": 1166,
                         "4_bez_zakrytiya": 1189, "10_bez_zakrytiya": 1096}
ZHDEM_PRODAZHA_KRIVOJ_TYAZHELYJ = {"4_zakryvaem": 1365, "10_zakryvaem": 1272,
                                   "4_bez_zakrytiya": 1295,
                                   "10_bez_zakrytiya": 1202}
# ЗАКРЫВАТЬ ЛИ ТОКЕНОВЫЙ СЧЁТ ТОЙ ЖЕ ТРАНЗАКЦИЕЙ -- РЕШАЕТ ТИП, А НЕ ВЫЗЫВАЮЩИЙ
# (слово владельца 03.10, п.1б). Прежде умолчание было "закрывать всегда", и в
# боевом режиме (нонс + чужой чаевый) продажа LaunchHub и Pump AMM НЕ ВЛЕЗАЛА:
# 1236...1267 и 1234...1265 байт при пределе 1232, перебор 4...35 байт. Без
# закрытия -- 1197...1228 и 1195...1226, влезает. Рента остаётся на счёте и
# забирается уборщиком (bloom_close_on_sell), как полоса и делала до 02.10.
# CPMM закрывает счёт по-прежнему: у него и с закрытием 1127...1158.
# Кривая не влезает ни так, ни иначе (1388...1420 без закрытия) -- ей нужны ещё
# и десять адресов в таблице, и это отдельное решение владельца.
ZAKRYVAT_SCHET_TOKENA_PO_TIPU = {
    PROG_CPMM: True,
    PROG_LAUNCHLAB: False,
    PROG_PUMP_AMM: False,
    PROG_KRIVAYA: False,
}


def zakryvat_schet_tokena_po_tipu(programma: str | None) -> bool:
    """Закрывать ли счёт у ЭТОГО типа. Незнакомый тип -- закрываем, как прежде."""
    return bool(ZAKRYVAT_SCHET_TOKENA_PO_TIPU.get(programma or "", True))


KOSHELEK_PROVERKI = "D3JuFoSXuWEMUUdCtoB5NYWnN87vjJSHtDP5rTD6qnph"
DRUGOJ_KOSHELEK = "4dPZMbReSobZVxfrzGLcD7xJN33pZhuUZix5HkTBTh4x"
# Живой образец кривой с котировочным минтом -- тот, на котором измерены места и
# арифметика. Подпись названа, чтобы проверку можно было повторить руками.
OBRAZEC_KRIVOJ = "3bn6wPKuLm62t7nqfK8u9iABz8zpAQQhLhznZiPSsRN8nasZj7Sizo2gJxT55i2ptPvfvatuuQGdJm7wZQb8dWDe"
KRIVAYA_RASHOZHDENIE = 875_307
KRIVAYA_VYHOD_SOBYTIYA = 26_277_828_094_315


# ЖИВЫЕ СДЕЛКИ БЕРУТСЯ НЕ ТОЛЬКО ИЗ ОБРАЗЦОВ ПУЛОВ. Покупки кривой v2 с
# котировочным минтом лежат ещё в двух файлах репозитория, и пропустить их значило
# бы проверить раскладку на одной сделке вместо трёх. Обход этих файлов -- по
# КАЖДОЙ инструкции (у записей там своя форма и свои поля), с дедупом по подписи.
DOP_FAJLY_OBRAZCOV = ("bloom_regression_txs.json",
                      "c3_usdc_noga/obrazcy_usdc_noga.json")


def _vse_tranzakcii(o, глубина: int = 0):
    """Все транзакции (meta + transaction) внутри любой вложенности файла."""
    if глубина > 6:
        return
    if isinstance(o, dict):
        if "meta" in o and "transaction" in o:
            yield o
        for v in o.values():
            yield from _vse_tranzakcii(v, глубина + 1)
    elif isinstance(o, list):
        for v in o[:500]:
            yield from _vse_tranzakcii(v, глубина + 1)


def _obrazcy(programma: str) -> list:
    """Живые сделки типа из образцов репозитория -- теми же глазами, что у полосы."""
    C, B, UN, _TS, _SB, _K = _moduli()
    out, bylo = [], set()

    def _vzyat(tx, vault, mint=None, source=None):
        if not isinstance(tx, dict) or not vault:
            return
        shab = B.extract_template(tx, programma, vault)
        if not shab.get("ok"):
            return
        mv = B.mints_and_vaults(shab, tx) or {}
        q = mv.get("quote_mint")
        if (B.spec_of(shab) or {}).get("native_quote") or q in (None, WSOL):
            return
        klyuch = (C.first_signature(tx), vault)
        if klyuch in bylo:
            return
        bylo.add(klyuch)
        out.append({"tx": tx, "vault": vault, "mint": mint or mv.get("base_mint"),
                    "quote": q, "quote_program": mv.get("quote_program"),
                    "source": source, "tpl": shab})

    for r in B.load_samples(programma):
        _vzyat(r.get("tx"), r.get("pool_vault"), r.get("mint"), r.get("source"))
    spec = B.SPECS.get(programma) or {}
    for imya in DOP_FAJLY_OBRAZCOV:
        put_ = Path(C.DATA) / imya
        if not put_.exists():
            continue
        try:
            d = json.loads(put_.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for tx in _vse_tranzakcii(d):
            ak = (((tx.get("transaction") or {}).get("message") or {})
                  .get("accountKeys") or [])
            if ak and isinstance(ak[0], str):
                try:
                    tx = UN.tx_s_adresami(tx)
                except Exception:  # noqa: BLE001, S112
                    continue
            for ix in B.all_instructions(tx):
                if ix.get("programId") != programma:
                    continue
                try:
                    dannye = B.b58decode(ix["data"])
                except Exception:  # noqa: BLE001, S112
                    continue
                vi = _mesto_kotirovki(B, programma, dannye)
                vi = (spec.get("quote_vault") if programma != PROG_KRIVAYA
                      else (B.BONDING_DISCS.get(dannye[:8].hex())
                            or {}).get("spec", {}).get("quote_vault"))
                if not isinstance(vi, int) or len(ix["accounts"]) <= vi:
                    continue
                _vzyat(tx, ix["accounts"][vi])
    return out


# ЖИВЫЕ ИНСТРУКЦИИ С КОТИРОВКОЙ РОВНО USDC -- СКОЛЬКО ИХ ЕСТЬ ВООБЩЕ. Ищутся не
# по полю записи образца (там стоит котировка ДРУГОГО плеча маршрута), а разбором
# КАЖДОЙ инструкции каждой сделки: место минта котировки берётся из раскладки
# типа. Сплошной обход 830 файлов data/ (включая ветки Code-1 и Code-2, слитые в
# эту) нашёл РОВНО ОДНУ такую инструкцию на все четыре типа -- Pump AMM
# ZDadp1kxj3q46yU8..., пул PUMP/USDC внутри транзакции роутера FLASHX8. Здесь
# обход идёт по образцам пулов: он быстрый, повторяемый и даёт то же самое.
ZHDEM_USDC_ZHIVYH = {PROG_CPMM: 0, PROG_LAUNCHLAB: 0, PROG_PUMP_AMM: 1,
                     PROG_KRIVAYA: 0}
OBRAZEC_USDC_PUMP_AMM = "ZDadp1kxj3q46yU8TUqiVsxHycyuzHoTDh3fQ844XFB9ThP1iK1kY5WTDMS4F6f3V1hnwuajnEWHFA17mxsWGq4"


def _mesto_kotirovki(B, programma: str, data: bytes):
    """Место минта котировки в раскладке этого типа (у кривой -- по разновидности)."""
    spec = B.SPECS.get(programma) or {}
    if programma == PROG_KRIVAYA:
        var = B.BONDING_DISCS.get(data[:8].hex()) or {}
        return ((var.get("spec") or {}).get("quote_mint"))
    return spec.get("quote_mint")


def _obrazcy_usdc(programma: str) -> list:
    """Живые инструкции этого типа, у которых котировка РОВНО USDC."""
    C, B, _UN, _TS, _SB, _K = _moduli()
    out, bylo = [], set()
    for r in B.load_samples(programma):
        tx = r.get("tx")
        if not isinstance(tx, dict):
            continue
        for ix in B.all_instructions(tx):
            if ix.get("programId") != programma:
                continue
            try:
                data = B.b58decode(ix["data"])
            except Exception:  # noqa: BLE001, S112
                continue
            qi = _mesto_kotirovki(B, programma, data)
            if not isinstance(qi, int) or len(ix["accounts"]) <= qi:
                continue
            if ix["accounts"][qi] != USDC:
                continue
            klyuch = (C.first_signature(tx), tuple(ix["accounts"][:3]))
            if klyuch in bylo:
                continue
            bylo.add(klyuch)
            spec = B.SPECS.get(programma) or {}
            vi = spec.get("quote_vault")
            out.append({"tx": tx, "sig": C.first_signature(tx),
                        "vault": (ix["accounts"][vi] if isinstance(vi, int)
                                  and len(ix["accounts"]) > vi else None),
                        "schetov": len(ix["accounts"]), "disc": data[:8].hex()})
    return out


def _zapis_nogi_1():
    """Статичный шаблон первой ноги SOL -> USDC из репозитория. Без сети."""
    import bloom_nogi_shablony as NSH  # noqa: PLC0415
    C, _B, _UN, _TS, _SB, _K = _moduli()
    put_ = Path(C.DATA) / "nogi_shablony.json"
    if not put_.exists():
        return None
    obr = (json.loads(put_.read_text(encoding="utf-8")).get("shablony") or {}).get(USDC)
    if not obr:
        return None
    zap = NSH.запись_из_образца(USDC, obr)
    if not isinstance(zap, dict):
        return None
    return dict(zap, price_sol=zap["price_sol_obrazca"], checked_at=time.time())


def _lut_s_dobavkoj(dobavka=()):
    from solders.address_lookup_table_account import AddressLookupTableAccount  # noqa: PLC0415
    from solders.pubkey import Pubkey  # noqa: PLC0415
    t = tablica_polosy()
    adr = list(dict.fromkeys(list(t["adresa"]) + [x for x in dobavka if x]))
    return AddressLookupTableAccount(
        Pubkey.from_string(t["klyuch"]),
        [Pubkey.from_string(a) for a in adr]), len(adr)


def self_test() -> int:  # noqa: C901, PLR0912, PLR0915
    import os  # noqa: PLC0415

    C, B, UN, TS, SB, K = _moduli()
    было, плохо = 0, 0

    def chk(имя, усл, факт=None):
        nonlocal было, плохо
        было += 1
        if усл:
            print(f"  ok   {имя}")
        else:
            плохо += 1
            print(f" ПЛОХО {имя} -- {факт!r}")

    print("c3_usdc_noga_signaly: самопроверка")

    # ------------------------------------------------- 1. таблица типов
    chk("типов в таблице четыре -- те, куда идут USDC-сигналы", len(TIPY) == 4,
        len(TIPY))
    chk("у каждого типа назван котировщик",
        all(kotirovshchik(p) for p in TIPY), [p for p in TIPY if not kotirovshchik(p)])
    chk("сумма сигналов по типам -- 114 из 118 (счёт Code-1)",
        sum(SIGNALOV_V_SUTKI.values()) == 114, sum(SIGNALOV_V_SUTKI.values()))
    # ПЕРЕСЕЧЕНИЕ С c2_usdc_noga -- НАЗВАНО, А НЕ СЛУЧАЙНО. 03.10 Code-1 добавил
    # CPMM в свою таблицу (37 % потока USDC), и теперь тип стоит в двух таблицах.
    # Это не беда и не дубль на деньгах: врезка зовёт сначала его путь и берётся
    # за мой ТОЛЬКО если тот не собрался (см. страницу врезки, диф №2). Здесь
    # CPMM остаётся ради переворота сторон и ради ПРОДАЖИ, которых у него нет.
    chk(f"пересечение таблиц с c2_usdc_noga -- ровно {sorted(PERESECHENIE_S_UN)}",
        set(TIPY) & set(UN.TYPES) == set(PERESECHENIE_S_UN),
        sorted(set(TIPY) & set(UN.TYPES)))
    chk("и у общего типа способ котировки у обоих один -- кирпичи",
        all(UN.KOTIROVSHCHIKI.get(p_) == UN.SPOSOB_KIRPICHI
            and kotirovshchik(p_) == SPOSOB_REZERVY for p_ in PERESECHENIE_S_UN),
        [(p_, UN.KOTIROVSHCHIKI.get(p_), kotirovshchik(p_))
         for p_ in PERESECHENIE_S_UN])
    ст = storona({}, programma="НеПрограмма", pul="x")
    chk("незнакомый тип -- отказ по имени, а не исключение",
        not ст["ok"] and WHY_TIP in (ст["why_not"] or ""), ст.get("why_not"))
    ст = storona({}, programma=PROG_CPMM)
    chk("без хранилища и пула -- отказ словами",
        not ст["ok"] and "ни хранилища" in (ст["why_not"] or ""), ст.get("why_not"))
    chk("режим -- тот же флаг, что у USDC-ноги", rezhim() == UN.rezhim(),
        (rezhim(), UN.rezhim()))
    chk("предел CU -- тот же, что у USDC-ноги", cu_nogi() == UN.cu_nogi(),
        (cu_nogi(), UN.cu_nogi()))

    # ------------------------------------------------- 2. таблица адресов полосы
    т = tablica_polosy()
    chk(f"таблица адресов полосы читается из data/{FAJL_TABLICY}", т["ok"],
        т.get("why_not"))
    chk(f"адресов в таблице {ADRESOV_V_TABLICE}",
        т["ok"] and len(т["adresa"]) == ADRESOV_V_TABLICE,
        len(т["adresa"]) if т["ok"] else None)
    chk("ключ таблицы -- тот, что создан и сверен по цепи Code-1",
        т["ok"] and т["klyuch"] == TABLICA_ADRESOV, т.get("klyuch"))
    нет_в_таблице = [TIPY[p]["label"] for p in TIPY if т["ok"] and p not in т["adresa"]]
    chk("программы всех четырёх типов уже лежат в таблице полосы",
        т["ok"] and not нет_в_таблице, нет_в_таблице)
    chk("минт USDC лежит в таблице полосы", т["ok"] and USDC in т["adresa"], None)

    # ------------------------------------------------- 3. по типам на живых сделках
    байт_всего = 0
    образцы_по_типам = {}
    for p, ждём in ZHDEM_PO_TIPAM.items():
        метка = TIPY[p]["label"]
        обр = _obrazcy(p)
        образцы_по_типам[p] = обр
        chk(f"{метка}: живых сделок с котировочным токеном {ждём['obrazcov']}",
            len(обр) == ждём["obrazcov"], len(обр))
        сторона_ок, байт, целиком, цена, отказ_цены = 0, 0, 0, 0, 0
        вне_наших, причины = [], []
        писать_сошлось, подписант_один = 0, 0
        for о in обр:
            ст = storona(о["tx"], programma=p, hranilishche=о["vault"],
                         mint_kotirovki=о["quote"])
            if not ст["ok"]:
                причины.append(ст["why_not"])
                continue
            сторона_ок += 1
            u = polzovatel_istochnika(ст, о["tx"])
            if not u:
                вне_наших.append(("пользователь источника не найден", None))
                continue
            a0, a1 = struct.unpack_from("<QQ", ст["tpl"]["data"], 8)
            наш = instrukciya_nogi(ст, user=u, amount_in=a0, min_out=a1,
                                   tx_istochnika=о["tx"], kak_u_istochnika=True)
            другой = instrukciya_nogi(ст, user=DRUGOJ_KOSHELEK, amount_in=a0,
                                      min_out=a1, tx_istochnika=о["tx"],
                                      kak_u_istochnika=True)
            получили = [str(м.pubkey) for м in наш.accounts]
            иначе = [str(м.pubkey) for м in другой.accounts]
            его = list(ст["tpl"]["accounts"])
            наши_места = {и for и, (x, y) in enumerate(zip(получили, иначе)) if x != y}
            разн = [и for и, (x, y) in enumerate(zip(его, получили)) if x != y]
            вне = [и for и in разн if и not in наши_места]
            данные = bytes(наш.data) == bytes(ст["tpl"]["data"])
            if данные and not вне:
                байт += 1
                if not разн:
                    целиком += 1
            else:
                вне_наших.append((C.first_signature(о["tx"])[:10], (данные, вне)))
            # ПРИЗНАК ЗАПИСИ СВЕРЯЕТСЯ ТОЛЬКО ВНЕ НАШИХ МЕСТ -- НА НАШИХ ОН
            # ТАВТОЛОГИЧЕН. Кирпич полосы ставит нашим счетам writable=True
            # всегда (is_user or writable), и у ПОДСТАВНОГО кошелька этих
            # адресов в транзакции источника нет вовсе -- сверять там нечего.
            # А вот счета ПУЛА и программы обязаны совпасть признаком тоже:
            # лишний writable на чужом счёте -- это отказ программы.
            if not [и for и in range(len(получили)) if и not in наши_места
                    and наш.accounts[и].is_writable
                    != bool(ст["tpl"]["writable"].get(получили[и], False))]:
                писать_сошлось += 1
            if len([м for м in наш.accounts if м.is_signer]) == 1:
                подписант_один += 1
            мо = min_out_boj(ст, о["tx"], amount_in=1_000_000, proskalzyvanie=0.35,
                             mint_kotirovki=о["quote"])
            if мо["ok"]:
                цена += 1
            else:
                отказ_цены += 1
                причины.append(мо["why_not"])
        байт_всего += байт
        chk(f"{метка}: сторона USDC разобралась на {ждём['storona']}",
            сторона_ок == ждём["storona"], (сторона_ок, причины[:2]))
        chk(f"{метка}: байт в байт на {ждём['bajt']}", байт == ждём["bajt"],
            (байт, вне_наших[:2]))
        chk(f"{метка}: ни одного расхождения ВНЕ наших мест", not вне_наших,
            вне_наших[:2])
        chk(f"{метка}: признак записи ВНЕ наших мест совпал с источником везде",
            писать_сошлось == ждём["storona"], (писать_сошлось, ждём["storona"]))
        chk(f"{метка}: подписант в инструкции РОВНО один и это наш кошелёк",
            подписант_один == ждём["storona"], (подписант_один, ждём["storona"]))
        chk(f"{метка}: цена посчиталась на {ждём['cena']}", цена == ждём["cena"],
            цена)
        chk(f"{метка}: отказов цены {ждём['otkaz_ceny']}, и у каждого есть причина",
            отказ_цены == ждём["otkaz_ceny"] and all(причины), отказ_цены)
    chk(f"байт в байт всего {ZHDEM_BAJT_VSEGO} живых сделок",
        байт_всего == ZHDEM_BAJT_VSEGO, байт_всего)

    # ------------------------- 3б. живые образцы с котировкой РОВНО USDC
    всего_usdc = 0
    for p, ждём_n in ZHDEM_USDC_ZHIVYH.items():
        метка = TIPY[p]["label"]
        у = _obrazcy_usdc(p)
        всего_usdc += len(у)
        chk(f"{метка}: живых инструкций с котировкой РОВНО USDC -- {ждём_n}",
            len(у) == ждём_n, len(у))
        for о in у:
            ст = storona(о["tx"], programma=p, hranilishche=о["vault"],
                         mint_kotirovki=USDC)
            chk(f"{метка}: USDC-образец {о['sig'][:10]} -- сторона разобралась",
                ст["ok"], ст.get("why_not"))
            if not ст["ok"]:
                continue
            u = polzovatel_istochnika(ст, о["tx"])
            a0, a1 = struct.unpack_from("<QQ", ст["tpl"]["data"], 8)
            наш = instrukciya_nogi(ст, user=u, amount_in=a0, min_out=a1,
                                   tx_istochnika=о["tx"], kak_u_istochnika=True)
            получили = [str(м.pubkey) for м in наш.accounts]
            chk(f"{метка}: USDC-образец {о['sig'][:10]} -- байт в байт ЦЕЛИКОМ",
                bytes(наш.data) == bytes(ст["tpl"]["data"])
                and получили == list(ст["tpl"]["accounts"]),
                [и for и, (x, y) in enumerate(zip(ст["tpl"]["accounts"],
                                                  получили)) if x != y])
            мо = min_out_boj(ст, о["tx"], amount_in=1_000_000,
                             proskalzyvanie=0.35)
            chk(f"{метка}: USDC-образец {о['sig'][:10]} -- цена посчиталась",
                мо["ok"] and мо["min_out"] > 0, мо.get("why_not"))
    chk("живых USDC-инструкций на все четыре типа -- ровно одна",
        всего_usdc == 1, всего_usdc)
    chk("и это тот самый образец Pump AMM (пул PUMP/USDC в маршруте роутера)",
        (_obrazcy_usdc(PROG_PUMP_AMM) or [{}])[0].get("sig")
        == OBRAZEC_USDC_PUMP_AMM,
        (_obrazcy_usdc(PROG_PUMP_AMM) or [{}])[0].get("sig"))

    # ------------------------------------------------- 4. места кривой v2
    все_кр = образцы_по_типам.get(PROG_KRIVAYA) or []
    chk("живых покупок кривой с котировочным минтом -- три",
        len(все_кр) == 3, len(все_кр))
    # ОБРАЗЕЦ ДЛЯ ЗАМЕРОВ ВЫБИРАЕТСЯ ПО ПОДПИСИ, А НЕ ПО ПОРЯДКУ: порядок
    # образцов зависит от файлов, а числа ниже измерены на ЭТОЙ сделке.
    кр = [о for о in все_кр if C.first_signature(о["tx"]) == OBRAZEC_KRIVOJ]
    chk("образец, на котором измерены места и арифметика, на месте", len(кр) == 1,
        [C.first_signature(о["tx"])[:12] for о in все_кр])
    if кр:
        о = кр[0]
        ст = storona(о["tx"], programma=PROG_KRIVAYA, hranilishche=о["vault"],
                     mint_kotirovki=о["quote"])
        сч = ст["tpl"]["accounts"]
        u = сч[KRIVAYA_V2_MESTA["user"]]
        пр = mesta_krivoj_v2(сч, nash_koshelek=u)
        chk("места кривой v2 вывелись и сошлись с покупкой источника 5 из 5",
            пр["ok"] and пр["soshlos"] == 5 and пр["provereno"] == 5,
            (пр.get("soshlos"), пр.get("provereno"), пр.get("why_not")))
        # СВЯЗАННЫЙ НАКОПИТЕЛЬ -- ОТДЕЛЬНОЙ ПРОВЕРКОЙ, ПОТОМУ ЧТО В ОБЩЕМ МОДУЛЕ
        # НАПИСАНО, ЧТО ОН НЕ ВЫВОДИТСЯ. Выводится: ATA накопителя по минту котировки.
        uva = B.pda([SEMYA_UVA, "USER"], u, PROG_KRIVAYA)
        chk("место 20 -- PDA(user_volume_accumulator + кошелёк)",
            uva == сч[KRIVAYA_V2_MESTA["uva"]], (uva, сч[KRIVAYA_V2_MESTA["uva"]]))
        асс = B.ata(uva, сч[KRIVAYA_V2_MESTA["mint_kotirovki"]],
                    сч[KRIVAYA_V2_MESTA["prog_kotirovki"]])
        chk("место 21 -- ATA(место 20, минт котировки, программа котировки), а не состояние",
            асс == сч[KRIVAYA_V2_MESTA["assoc_uva"]],
            (асс, сч[KRIVAYA_V2_MESTA["assoc_uva"]]))
        # КИРПИЧ ТЕПЕРЬ ВЫВОДИТ ЭТИ МЕСТА САМ -- ДИФ №3 ВЗЯТ (Code-1, 03.10).
        # До правки c2_swap_build.user_accounts нашим кошельком отдавал пустой
        # словарь (шёл за ключом места 21 в состояние, которого для USDC не
        # будет никогда), и 03.10 это стоило 16 отказов «роли счетов не
        # восстановились» на источнике cand1_03 с билетом 0.5. Теперь два
        # независимых вывода -- его и мой -- обязаны дать РОВНО ОДНО И ТО ЖЕ;
        # расхождение значит, что один из них сломался, и это провал.
        наши5 = mesta_krivoj_v2(сч, nash_koshelek=KOSHELEK_PROVERKI)
        кирпич5 = B.user_accounts(ст["tpl"], о["tx"], KOSHELEK_PROVERKI)
        chk("свои места кривой v2 восстанавливаются все пять, без состояния",
            наши5["ok"] and len(наши5["mesta"]) == 5, наши5.get("why_not"))
        chk("кирпич после дифа №3 выводит те же пять мест, значение в значение",
            кирпич5 == наши5["mesta"], (кирпич5, наши5.get("mesta")))
        # ЦЕНА: СОБЫТИЕ ВОСПРОИЗВЕЛО СВОЮ СДЕЛКУ КОТИРОВОЧНОЙ СТОРОНОЙ
        ев = sobytie_krivoj(о["tx"], о["mint"])
        chk("событие кривой разобралось и котировка в нём токеновая",
            ев["ok"] and ев["tokenovaya"] and ев["quote_mint"] == о["quote"],
            (ев.get("why_not"), ев.get("quote_mint")))
        if ев["ok"]:
            п = ев["polya"]
            до = int(п["virtual_quote_reserves"]) - int(п["quote_amount"])
            вт = int(п["virtual_token_reserves"]) + int(п["token_amount"])
            трата = int(п["quote_amount"]) + int(п["fee"]) + int(п["creator_fee"])
            вх = K.вход_кривой(котировки=трата, вирт_котировка=до, вирт_токены=вт,
                               fee_bps=int(п["fee_basis_points"]),
                               creator_bps=int(п["creator_fee_basis_points"]))
            chk("до кривой доходит РОВНО quote_amount сделки источника",
                вх["ok"] and вх["до_кривой"] == int(п["quote_amount"]),
                (вх.get("до_кривой"), п["quote_amount"]))
            chk("комиссии сошлись с ceil(quote * bps) до единицы",
                вх["комиссия"] == int(п["fee"])
                and вх["комиссия_создателя"] == int(п["creator_fee"]),
                (вх.get("комиссия"), п["fee"], вх.get("комиссия_создателя"),
                 п["creator_fee"]))
            chk(f"кривая воспроизвела выход с расхождением {KRIVAYA_RASHOZHDENIE}",
                вх["выход"] - KRIVAYA_VYHOD_SOBYTIYA == KRIVAYA_RASHOZHDENIE,
                вх["выход"] - KRIVAYA_VYHOD_SOBYTIYA)
            chk("расхождение меньше допуска 1e-6 от выхода",
                KRIVAYA_RASHOZHDENIE * 1_000_000 < KRIVAYA_VYHOD_SOBYTIYA, None)
            ст_чужая = min_out_boj(ст, о["tx"], amount_in=1_000_000,
                                   proskalzyvanie=0.35, mint_kotirovki=USDC)
            chk("котировка кривой не тем минтом, которым платит нога -- отказ по имени",
                not ст_чужая["ok"]
                and WHY_KOTIROVKA_SOBYTIYA in (ст_чужая["why_not"] or ""),
                ст_чужая.get("why_not"))
    # 18-счётная разновидность -- отказ по имени
    v1 = [r for r in B.load_samples(PROG_KRIVAYA)
          if isinstance(r.get("tx"), dict)
          and (B.extract_template(r["tx"], PROG_KRIVAYA, r.get("pool_vault")) or {})
          .get("ix") in ("66063d1201daebea", "38fc74089edfcd5f")]
    chk("18-счётные покупки кривой в образцах есть", len(v1) >= 5, len(v1))
    if v1:
        ст1 = storona(v1[0]["tx"], programma=PROG_KRIVAYA,
                      hranilishche=v1[0]["pool_vault"], mint_kotirovki=USDC)
        chk("кривая с нативной котировкой -- отказ по имени, а не сборка",
            not ст1["ok"] and WHY_KRIVAYA_V1 in (ст1["why_not"] or ""),
            ст1.get("why_not"))

    # ------------------------------------------------- 5. переворот сторон CPMM
    цп = образцы_по_типам.get(PROG_CPMM) or []
    if цп:
        о = цп[0]
        ст_п = storona(о["tx"], programma=PROG_CPMM, hranilishche=о["vault"],
                       mint_kotirovki=о["mint"])
        chk("CPMM: просим котировку с выходной стороны -- стороны переставились",
            ст_п["ok"] and ст_п["razvernut"], (ст_п.get("ok"), ст_п.get("why_not")))
        if ст_п["ok"]:
            chk("CPMM: после переворота на месте 10 стоит та котировка, что просили",
                ст_п["tpl"]["accounts"][10] == о["mint"],
                ст_п["tpl"]["accounts"][10])
            дважды = TS.перевернуть_cpmm(ст_п["tpl"], о["quote"])
            chk("CPMM: два переворота -- тот же список счетов (инволюция)",
                list(дважды["accounts"]) == list(о["tpl"]["accounts"]), None)
            мо = min_out_boj(ст_п, о["tx"], amount_in=1_000_000,
                             proskalzyvanie=0.35, mint_kotirovki=о["mint"])
            chk("CPMM развёрнутый: цена ОТКАЗЫВАЕТ по имени (обратный своп)",
                not мо["ok"] and WHY_OTHER_SIDE in (мо["why_not"] or ""),
                мо.get("why_not"))
    # Pump AMM и LaunchLab переворачивать нечем -- отказ по имени
    лл = образцы_по_типам.get(PROG_LAUNCHLAB) or []
    if лл:
        ст_л = storona(лл[0]["tx"], programma=PROG_LAUNCHLAB,
                       hranilishche=лл[0]["vault"], mint_kotirovki=лл[0]["mint"])
        chk("LaunchLab: обратная сторона -- отказ по имени, переворота там нет",
            not ст_л["ok"] and WHY_NO_REVERSE in (ст_л["why_not"] or ""),
            ст_л.get("why_not"))

    # ------------------------------------------------- 6. список инструкций и размер
    зап = _zapis_nogi_1()
    chk("статичный шаблон первой ноги SOL -> USDC в репозитории есть",
        isinstance(зап, dict) and зап.get("q_dec") == 6
        and зап.get("program") == PROG_PUMP_AMM,
        (зап or {}).get("program"))
    сохр = {k: os.environ.get(k) for k in (UN.FLAG, UN.FLAG_GROUPS)}
    os.environ[UN.FLAG] = UN.MODE_LIVE
    os.environ.pop(UN.FLAG_GROUPS, None)
    try:
        порядок_ок, аргументы_ок, размеры = 0, 0, {}
        for p, обр in образцы_по_типам.items():
            размеры[p] = {р: [] for р in REZHIMY_PAKETA}
            for о in обр:
                кэш = SB.LegCache({}, None)
                # В КЭШ ПОЛОЖЕН РЕАЛЬНЫЙ ШАБЛОН НОГИ SOL -> USDC ПОД КЛЮЧОМ
                # КОТИРОВКИ ОБРАЗЦА. Это замер РАЗМЕРА и ПОРЯДКА, а не цены:
                # экономической связки между ногами тут нет (котировки разные), и
                # она проверяется отдельно -- на USDC-образцах, которых по этим
                # четырём типам ещё нет. Размер от минта котировки не зависит
                # ничем, кроме попадания адреса в таблицу, и это посчитано ниже
                # отдельной строкой ("usdc").
                кэш.entries[о["quote"]] = зап
                ист = о["source"] or (sorted(C.signers(о["tx"]))[0]
                                      if C.signers(о["tx"]) else None)
                рез = instrukcii(tx_istochnika=о["tx"], istochnik=ист, mint=о["mint"],
                                 nash_koshelek=KOSHELEK_PROVERKI, lamporty=10_000_000,
                                 kesh_nog=кэш, proskalzyvanie=0.35,
                                 chaevye_lamporty=0, chaevye_adres=None,
                                 mint_kotirovki=о["quote"], min_out_vneshnij=1)
                if not рез["ok"]:
                    continue
                ixs = рез["ixs"]
                ждём_прог = ["ComputeBudget111111111111111111111111111111",
                             "ComputeBudget111111111111111111111111111111",
                             B.ATA_PROGRAM, B.ATA_PROGRAM, B.ATA_PROGRAM,
                             B.SYSTEM, (зап["mv"] or {}).get("quote_program"),
                             зап["program"], рез["pool_program"]]
                if len(ixs) == 9 and [str(i.program_id) for i in ixs] == ждём_прог \
                        and bytes(ixs[0].data)[:1] == b"\x02" \
                        and bytes(ixs[1].data)[:1] == b"\x03" \
                        and all(bytes(ixs[j].data) == b"\x01" for j in (2, 3, 4)) \
                        and bytes(ixs[6].data)[:1] == b"\x11":
                    порядок_ок += 1
                l1 = struct.unpack_from("<QQ", bytes(ixs[7].data), 8)
                l2 = struct.unpack_from("<QQ", bytes(ixs[8].data), 8)
                if l1 == (10_000_000, рез["leg1_min_out"]) \
                        and l2 == (рез["leg2_amount_in"], рез["min_out"]) \
                        and рез["leg2_amount_in"] == рез["leg1_min_out"]:
                    аргументы_ок += 1
                нашлось = B.ata(KOSHELEK_PROVERKI, о["quote"], о["quote_program"])
                # ОДНА ТАБЛИЦА НА ВСЕ ЧЕТЫРЕ РЕЖИМА: добавка в неё -- те три
                # адреса, которые на USDC-котировке в ней уже лежат. Меняется
                # только то, что полоса кладёт в транзакцию: чаевые и нонс.
                L, _n = _lut_s_dobavkoj((о["quote"], нашлось, о["quote_program"]))
                for имя, кв in (
                        ("chaevyj_nash", {"chaevye_adres": KOSHELEK_PROVERKI,
                                          "chaevye_lamporty": 1_000_000}),
                        ("chaevyj_chuzhoj", {"chaevye_adres": TIP_DLYA_ZAMERA,
                                             "chaevye_lamporty": 1_000_000}),
                        ("nons", {"chaevye_adres": None, "chaevye_lamporty": 0,
                                  "nons": (NONS_DLYA_ZAMERA, KOSHELEK_PROVERKI)}),
                        ("nons_i_chaevyj", {"chaevye_adres": TIP_DLYA_ZAMERA,
                                            "chaevye_lamporty": 1_000_000,
                                            "nons": (NONS_DLYA_ZAMERA,
                                                     KOSHELEK_PROVERKI)})):
                    сб = sobrat(tx_istochnika=о["tx"], istochnik=ист, mint=о["mint"],
                                nash_koshelek=KOSHELEK_PROVERKI, lamporty=10_000_000,
                                kesh_nog=кэш, proskalzyvanie=0.35,
                                min_out_vneshnij=1, mint_kotirovki=о["quote"],
                                luts_gotovye=[L], **кв)
                    if isinstance(сб.get("size"), int):
                        размеры[p][имя].append(сб["size"])
        собралось = sum(len(v["chaevyj_nash"]) for v in размеры.values())
        chk("порядок инструкций -- девять, место в место как у c2_usdc_noga",
            порядок_ок == собралось and собралось > 0, (порядок_ок, собралось))
        chk("суммы связаны: вход второй ноги -- это минимум первой",
            аргументы_ок == собралось, (аргументы_ок, собралось))
        for p, ждём in ZHDEM_RAZMEROV.items():
            метка = TIPY[p]["label"]
            for имя, (мин, макс) in ждём.items():
                ряд = размеры[p][имя]
                chk(f"{метка}: размер ({имя}) {мин}...{макс} байт",
                    ряд and min(ряд) == мин and max(ряд) == макс,
                    (min(ряд), max(ряд)) if ряд else None)
            # ВЕРДИКТ -- ПО САМОМУ ТЯЖЁЛОМУ РЕЖИМУ, а не по лёгкому: решать
            # по нижней границе значило бы обещать то, чего в бою не будет.
            влезли = [x for x in размеры[p]["nons_i_chaevyj"]
                      if x <= TS.ПРЕДЕЛ_РАЗМЕРА_TX]
            chk(f"{метка}: в самом тяжёлом режиме влезает "
                f"{ZHDEM_VLEZLO_TYAZHELYJ[p]} из {len(размеры[p]['nons_i_chaevyj'])}",
                len(влезли) == ZHDEM_VLEZLO_TYAZHELYJ[p],
                (len(влезли), len(размеры[p]["nons_i_chaevyj"])))
            chk(f"{метка}: тяжёлый режим ровно на 106 байт больше лёгкого",
                (min(размеры[p]["nons_i_chaevyj"]) - min(размеры[p]["chaevyj_nash"])
                 == 106
                 and max(размеры[p]["nons_i_chaevyj"])
                 - max(размеры[p]["chaevyj_nash"]) == 106),
                (min(размеры[p]["nons_i_chaevyj"]) - min(размеры[p]["chaevyj_nash"]),
                 max(размеры[p]["nons_i_chaevyj"]) - max(размеры[p]["chaevyj_nash"])))
        # ЛЕКАРСТВО КРИВОЙ -- ЧИСЛАМИ
        if кр:
            о = кр[0]
            кэш = SB.LegCache({}, None)
            кэш.entries[о["quote"]] = зап
            ист = о["source"] or sorted(C.signers(о["tx"]))[0]
            ст = storona(о["tx"], programma=PROG_KRIVAYA, hranilishche=о["vault"],
                         mint_kotirovki=о["quote"])
            сч = ст["tpl"]["accounts"]
            как_usdc = [о["quote"],
                        B.ata(KOSHELEK_PROVERKI, о["quote"], о["quote_program"]),
                        о["quote_program"]]
            наб = nabor_adresov_dlya_tablicy(сч, nash_koshelek=KOSHELEK_PROVERKI)
            chk(f"набор для таблицы: {ZHDEM_NABORA['vsego']} адресов, из них "
                f"{ZHDEM_NABORA['uzhe_v_tablice']} уже в ней",
                наб["ok"] and len(наб["nabor"]) == ZHDEM_NABORA["vsego"]
                and наб["svod"]["uzhe_v_tablice"] == ZHDEM_NABORA["uzhe_v_tablice"]
                and наб["svod"]["dobavit"] == ZHDEM_NABORA["dobavit"],
                наб.get("svod"))
            chk("оба НАШИХ получателя и их ATA для WSOL -- уже в таблице полосы",
                all(р["состояние"] == "уже в таблице" for р in наб["nabor"]
                    if р["что"].startswith(("получатель комиссии",
                                            "buyback-получатель",
                                            "ATA получателя для WSOL",
                                            "ATA buyback для WSOL"))),
                [р for р in наб["nabor"] if р["состояние"] != "уже в таблице"][:3])
            # ЛЕСТНИЦА: сколько адресов добавить, чтобы влезли покупка и продажа
            добавить = {
                0: [],
                4: наб["postojannye"],
                6: наб["postojannye"] + наб["ata_usdc_poluchatelej"],
                8: наб["postojannye"] + наб["ata_usdc_poluchatelej"]
                + наб["nash_nakopitel"],
            }
            for сколько, добавка in добавить.items():
                L, _n = _lut_s_dobavkoj(как_usdc + добавка)
                общее = {"tx_istochnika": о["tx"], "istochnik": ист,
                         "mint": о["mint"], "nash_koshelek": KOSHELEK_PROVERKI,
                         "lamporty": 10_000_000, "kesh_nog": кэш,
                         "proskalzyvanie": 0.35, "min_out_vneshnij": 1,
                         "mint_kotirovki": о["quote"], "luts_gotovye": [L]}
                тяж = {"chaevye_lamporty": 1_000_000,
                       "chaevye_adres": TIP_DLYA_ZAMERA,
                       "nons": (NONS_DLYA_ZAMERA, KOSHELEK_PROVERKI)}
                пк = sobrat(**общее, **тяж)
                пр_общее = {"tx_pokupki": о["tx"], "programma": PROG_KRIVAYA,
                            "nash_koshelek": KOSHELEK_PROVERKI,
                            "ostatok": 1_000_000_000, "kesh_nog": кэш,
                            "mint_bazy": о["mint"], "hranilishche": о["vault"],
                            "mint_kotirovki": о["quote"], "min_out_nogi_2": 1,
                            "luts_gotovye": [L]}
                пр = prodazha_sobrat(**пр_общее, **тяж)
                пр_б = prodazha_sobrat(**пр_общее, **тяж,
                                       zakryvat_schet_tokena=False)
                ждём = ZHDEM_KRIVAYA_LESTNICA[сколько]
                chk(f"кривая v2 (бой): +{сколько} адресов -> покупка "
                    f"{ждём['pokupka']}, продажа {ждём['prodazha']}, продажа "
                    f"без закрытия {ждём['prodazha_bez_zakrytiya']}",
                    (пк.get("size") == ждём["pokupka"]
                     and пр.get("size") == ждём["prodazha"]
                     and пр_б.get("size") == ждём["prodazha_bez_zakrytiya"]),
                    (пк.get("size"), пр.get("size"), пр_б.get("size")))
            chk("восьми адресов хватает И покупке, И продаже в боевом режиме",
                ZHDEM_KRIVAYA_LESTNICA[8]["pokupka"] <= TS.ПРЕДЕЛ_РАЗМЕРА_TX
                and ZHDEM_KRIVAYA_LESTNICA[8]["prodazha"]
                <= TS.ПРЕДЕЛ_РАЗМЕРА_TX, ZHDEM_KRIVAYA_LESTNICA[8])
            # ПОСЛЕ ДОЛИВКИ ТАБЛИЦЫ ХВАТАЕТ И НУЛЯ ДОБАВОК: четыре постоянные
            # программы кривой уже в ней, поэтому «+4» равно «+0» и обе
            # транзакции влезают. Остаток лестницы (+6, +8) -- не «чтобы
            # влезло», а запас: он снимает ещё 31 байт и нужен настоящей
            # сделке с котировкой USDC (её ATA получателей в таблице нет).
            chk("по ДОЛИТОЙ таблице влезают и покупка, и продажа БЕЗ добавок",
                ZHDEM_KRIVAYA_LESTNICA[0]["pokupka"] <= TS.ПРЕДЕЛ_РАЗМЕРА_TX
                and ZHDEM_KRIVAYA_LESTNICA[0]["prodazha"]
                <= TS.ПРЕДЕЛ_РАЗМЕРА_TX
                and ZHDEM_KRIVAYA_LESTNICA[4] == ZHDEM_KRIVAYA_LESTNICA[0],
                (ZHDEM_KRIVAYA_LESTNICA[0], ZHDEM_KRIVAYA_LESTNICA[4]))
    finally:
        for k, v in сохр.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------------------- 7. продажа токен -> USDC -> SOL
    зап2 = _zapis_nogi_1()
    н2 = noga_2_prodazhi(зап2 or {})
    chk("продажа второй ноги: зеркало нашей покупки ноги собралось",
        н2["ok"], н2.get("why_not"))
    chk("её покупка 25-счётная, продажа -- 23 счёта (места 19 и 20 выброшены)",
        н2["ok"] and н2["schetov_pokupki"] == 25 and len(н2["accounts"]) == 23,
        (н2.get("schetov_pokupki"), len(н2.get("accounts") or [])))
    chk("23-счётная продажа Pump AMM живым образцом НЕ проверена -- и это сказано",
        н2.get("zhivym_obrazcom_ne_provereno") is True,
        н2.get("zhivym_obrazcom_ne_provereno"))
    # МЕСТА 19 И 20 -- НЕ ПО ПАМЯТИ, А ВЫВОДОМ. 19 -- PDA global_volume_accumulator
    # (один на программу), 20 -- PDA user_volume_accumulator нашего кошелька. На
    # ВСЕХ живых покупках Pump AMM образцов (25 штук 26-счётных и одна
    # 25-счётная) они стоят РОВНО на этих местах -- значит выбрасывать их у
    # 25-счётной покупки так же законно, как у 26-счётной.
    сч1 = (зап2 or {}).get("tpl", {}).get("accounts") or []
    if сч1:
        from solders.pubkey import Pubkey as _Pk  # noqa: PLC0415
        гва = str(_Pk.find_program_address([b"global_volume_accumulator"],
                                           _Pk.from_string(PROG_PUMP_AMM))[0])
        ува = B.pda([SEMYA_UVA, "USER"], сч1[1], PROG_PUMP_AMM)
        chk("место 19 покупки ноги -- PDA global_volume_accumulator",
            сч1[19] == гва, (сч1[19], гва))
        chk("место 20 покупки ноги -- PDA user_volume_accumulator её кошелька",
            сч1[20] == ува, (сч1[20], ува))
        пары = 0
        for r in B.load_samples(PROG_PUMP_AMM):
            for ix in B.all_instructions(r.get("tx") or {}):
                if ix.get("programId") != PROG_PUMP_AMM:
                    continue
                try:
                    д = B.b58decode(ix["data"])
                except Exception:  # noqa: BLE001, S112
                    continue
                if д[:8] not in (B.disc("buy_exact_quote_in"), B.disc("buy")):
                    continue
                а = ix["accounts"]
                if len(а) < 25:
                    continue
                if а[19] == гва and а[20] == B.pda([SEMYA_UVA, "USER"], а[1],
                                                   PROG_PUMP_AMM):
                    пары += 1
        chk("на всех живых покупках Pump AMM места 19 и 20 именно эти -- 26 из 26",
            пары == 26, пары)
    chk("выброшены РОВНО места 19 и 20 покупки ноги",
        н2["ok"] and н2["accounts"] == [a for i, a in enumerate(зап2["tpl"]["accounts"])
                                        if i not in PUMP_AMM_MESTA_TOLKO_POKUPKI],
        None)
    os.environ[UN.FLAG] = UN.MODE_LIVE
    try:
        for p, ждём in ZHDEM_PRODAZHI.items():
            метка = TIPY[p]["label"]
            ряды = {"chaevyj_nash": [], "nons_i_chaevyj": [], "bez_zakrytiya": [],
                    "po_tipu": []}
            порядок, суммы = 0, 0
            без_мин = 0
            по_типу_ок = 0
            for о in образцы_по_типам.get(p) or []:
                кэш = SB.LegCache({}, None)
                кэш.entries[о["quote"]] = зап2
                общие = {"tx_pokupki": о["tx"], "programma": p,
                         "nash_koshelek": KOSHELEK_PROVERKI,
                         "ostatok": 1_000_000_000, "kesh_nog": кэш,
                         "mint_bazy": о["mint"], "hranilishche": о["vault"],
                         "mint_kotirovki": о["quote"]}
                без = prodazha_instrukcii(**общие)
                if not без["ok"] and WHY_NOGA2_NET_MIN in (без["why_not"] or ""):
                    без_мин += 1
                # ПОРЯДОК МЕРИТСЯ В РЕЖИМЕ С ЗАКРЫТИЕМ СЧЁТА, чтобы проверка
                # осталась про ПОРЯДОК, а не про число закрытий: закрывать ли
                # счёт -- это теперь решение по типу (п.1б владельца), и оно
                # проверяется отдельно ниже.
                рез = prodazha_instrukcii(**общие, min_out_nogi_2=1,
                                          zakryvat_schet_tokena=True)
                if not рез["ok"]:
                    continue
                ixs = рез["ixs"]
                ждём_прог = ["ComputeBudget111111111111111111111111111111",
                             "ComputeBudget111111111111111111111111111111",
                             B.ATA_PROGRAM, B.ATA_PROGRAM, p, PROG_PUMP_AMM]
                if len(ixs) == 8 \
                        and [str(i.program_id) for i in ixs[:6]] == ждём_прог \
                        and bytes(ixs[6].data)[:1] == b"\x09" \
                        and bytes(ixs[7].data)[:1] == b"\x09":
                    порядок += 1
                # ПО ТИПУ: у кого счёт не закрываем -- закрытие РОВНО одно (WSOL).
                _по_типу = prodazha_instrukcii(**общие, min_out_nogi_2=1)
                if _по_типу.get("ok"):
                    _закр = sum(1 for i in _по_типу["ixs"]
                                if bytes(i.data)[:1] == b"\x09")
                    _ждём_закр = 2 if zakryvat_schet_tokena_po_tipu(p) else 1
                    if (_закр == _ждём_закр
                            and _по_типу.get("zakryvaem_po_tipu") is True
                            and _по_типу.get("zakryvaem_schet_tokena")
                            is zakryvat_schet_tokena_po_tipu(p)):
                        по_типу_ок += 1
                if рез["noga_2_amount_in"] == рез["min_out_nogi_1"]:
                    суммы += 1
                нашлось = B.ata(KOSHELEK_PROVERKI, о["quote"], о["quote_program"])
                L, _n = _lut_s_dobavkoj((о["quote"], нашлось, о["quote_program"]))
                тяж = {"chaevye_adres": TIP_DLYA_ZAMERA,
                       "chaevye_lamporty": 1_000_000,
                       "nons": (NONS_DLYA_ZAMERA, KOSHELEK_PROVERKI)}
                for имя, кв in (
                        # ПРЕЖНИЕ ДВА РЕЖИМА -- С ЗАКРЫТИЕМ СЧЁТА ЯВНО: их числа
                        # остаются под проверкой, иначе правка умолчания скрыла
                        # бы их, а не проверила.
                        ("chaevyj_nash", {"chaevye_adres": KOSHELEK_PROVERKI,
                                          "chaevye_lamporty": 1_000_000,
                                          "zakryvat_schet_tokena": True}),
                        ("nons_i_chaevyj", dict(тяж,
                                                zakryvat_schet_tokena=True)),
                        ("bez_zakrytiya", dict(тяж,
                                               zakryvat_schet_tokena=False)),
                        # НОВОЕ УМОЛЧАНИЕ -- БЕЗ ФЛАГА ВОВСЕ: ровно то, что
                        # поедет в бой.
                        ("po_tipu", тяж)):
                    сб = prodazha_sobrat(**общие, min_out_nogi_2=1,
                                         luts_gotovye=[L], **кв)
                    if isinstance(сб.get("size"), int):
                        ряды[имя].append(сб["size"])
            ждём_сделок = (ZHDEM_PRODAZH_KRIVOJ if p == PROG_KRIVAYA
                           else len(образцы_по_типам[p]))
            chk(f"{метка}: продажа собралась на {ждём_сделок} живых сделках",
                порядок == ждём_сделок and порядок > 0,
                (порядок, ждём_сделок))
            chk(f"{метка}: вход второй ноги продажи -- это минимум первой",
                суммы == ждём_сделок, суммы)
            chk(f"{метка}: без минимума второй ноги -- отказ по имени, а не продажа",
                без_мин == ждём_сделок, (без_мин, ждём_сделок))
            for имя, (мин, макс) in ждём.items():
                ряд = ряды[имя]
                chk(f"{метка}: размер продажи ({имя}) {мин}...{макс} байт",
                    ряд and min(ряд) == мин and max(ряд) == макс,
                    (min(ряд), max(ряд)) if ряд else None)
            # ВЕРДИКТ ПРОДАЖИ -- ТОЖЕ ПО ТЯЖЁЛОМУ РЕЖИМУ. В режиме нонса
            # продажа не влезает у LaunchLab и Pump AMM (перебор 4...35 байт), и
            # лечится тем же, чем у кривой: не закрывать токеновый счёт этой же
            # транзакцией.
            влезли = [x for x in ряды["nons_i_chaevyj"]
                      if x <= TS.ПРЕДЕЛ_РАЗМЕРА_TX]
            chk(f"{метка}: продажа в тяжёлом режиме влезает "
                f"{ZHDEM_VLEZLO_PRODAZHA_TYAZHELYJ[p]} из "
                f"{len(ряды['nons_i_chaevyj'])}",
                len(влезли) == ZHDEM_VLEZLO_PRODAZHA_TYAZHELYJ[p],
                (len(влезли), len(ряды["nons_i_chaevyj"])))
            # ОСТАТОК USDC ВО ВТОРОЙ НОГЕ (добавка MRKL): передали число --
            # вход второй ноги на него больше, не передали -- как прежде, и
            # причина названа полем. Проверяется на тех же живых сделках.
            _ост = 12_345
            _с_ост = prodazha_instrukcii(**общие, min_out_nogi_2=1,
                                         ostatok_usdc=_ост)
            _без_ост = prodazha_instrukcii(**общие, min_out_nogi_2=1)
            if _с_ост.get("ok") and _без_ост.get("ok"):
                chk(f"{метка}: остаток USDC добавлен во вход второй ноги",
                    _с_ост["noga_2_amount_in"]
                    == _без_ост["noga_2_amount_in"] + _ост
                    and _с_ост["noga_2_ostatok_usdc"] == _ост,
                    (_без_ост["noga_2_amount_in"], _с_ост["noga_2_amount_in"]))
                chk(f"{метка}: без остатка -- вход РОВНО минимум первой ноги, "
                    "и причина названа",
                    _без_ост["noga_2_amount_in"] == _без_ост["min_out_nogi_1"]
                    and _без_ост["noga_2_ostatok_usdc"] is None
                    and "остаток USDC не передан"
                    in (_без_ост["noga_2_ostatok_why_not"] or ""),
                    _без_ост.get("noga_2_ostatok_why_not"))

            # ЗАКРЫТИЕ СЧЁТА -- ПО ТИПУ, И ЭТО ПРОВЕРЯЕТСЯ ЧИСЛОМ ЗАКРЫТИЙ, а
            # не только размером: у кого не закрываем -- закрытие РОВНО одно
            # (WSOL), и признак zakryvaem_po_tipu в ответе стоит.
            chk(f"{метка}: закрытие счёта по типу "
                f"({'закрываем' if zakryvat_schet_tokena_po_tipu(p) else 'НЕ закрываем'}) "
                f"на {ждём_сделок} сделках",
                по_типу_ок == ждём_сделок and по_типу_ок > 0,
                (по_типу_ок, ждём_сделок))
            # УМОЛЧАНИЕ В ТЯЖЁЛОМ РЕЖИМЕ -- ТО, ЧТО ПОЕДЕТ В БОЙ. У CPMM оно
            # равно режиму с закрытием, у остальных трёх -- режиму без него.
            _ждём_умолч = (ZHDEM_PRODAZHI[p]["nons_i_chaevyj"]
                           if zakryvat_schet_tokena_po_tipu(p)
                           else ZHDEM_PRODAZHI_BEZ_ZAKRYTIYA[p])
            _ряд_у = ряды["po_tipu"]
            chk(f"{метка}: умолчание (тяжёлый режим) {_ждём_умолч[0]}..."
                f"{_ждём_умолч[1]} байт",
                _ряд_у and (min(_ряд_у), max(_ряд_у)) == tuple(_ждём_умолч),
                (min(_ряд_у), max(_ряд_у)) if _ряд_у else None)
            _влезло_у = [x for x in _ряд_у if x <= TS.ПРЕДЕЛ_РАЗМЕРА_TX]
            # ОСОБОГО СЛУЧАЯ У КРИВОЙ БОЛЬШЕ НЕТ. Пока таблица была 34 адреса,
            # её умолчание не влезало ни одной сделкой; после доливки восьми
            # адресов (03.10, Code-1) влезают ВСЕ четыре типа, и проверка у всех
            # одна. Если таблица когда-нибудь уменьшится, это место покраснеет
            # числом, а не промолчит.
            chk(f"{метка}: умолчанием влезает {len(_влезло_у)} из {len(_ряд_у)}",
                len(_влезло_у) == len(_ряд_у) and bool(_влезло_у),
                (len(_влезло_у), len(_ряд_у)))
            мин_б, макс_б = ZHDEM_PRODAZHI_BEZ_ZAKRYTIYA[p]
            ряд_б = ряды["bez_zakrytiya"]
            chk(f"{метка}: продажа без закрытия счёта токена {мин_б}...{макс_б}",
                ряд_б and min(ряд_б) == мин_б and max(ряд_б) == макс_б,
                (min(ряд_б), max(ряд_б)) if ряд_б else None)
            влезли_б = [x for x in ряд_б if x <= TS.ПРЕДЕЛ_РАЗМЕРА_TX]
            chk(f"{метка}: продажа без закрытия влезает на всех сделках",
                len(влезли_б) == len(ряд_б) and bool(влезли_б),
                (len(влезли_б), len(ряд_б)))
        # ЛЕСТНИЦА ДОБАВОК ДЛЯ КРИВОЙ СТОИТ В РАЗДЕЛЕ ПОКУПКИ: она считает
        # покупку и продажу ОДНИМ набором адресов, и повторять её здесь незачем.
    finally:
        for k, v in сохр.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------- 8. почему не двухшаговым путём (числом)
    for p, ждём in ZHDEM_DVUHSHAGOVYJ.items():
        метка = TIPY[p]["label"]
        ряд, причины = [], []
        for о in образцы_по_типам.get(p) or []:
            кэш = SB.LegCache({}, None)
            кэш.entries[о["quote"]] = зап
            ист = о["source"] or (sorted(C.signers(о["tx"]))[0]
                                  if C.signers(о["tx"]) else None)
            дв = TS.собрать(tx_источника=о["tx"], источник=ист, минт=о["mint"],
                            наш_кошелёк=KOSHELEK_PROVERKI, лампорты=10_000_000,
                            кэш_ног=кэш, чаевые_лампорты=0, чаевые_адрес=None,
                            rpc_call=lambda _m, _p: {"value": []})
            причины.append(дв.get("why_not") or "")
            if isinstance(дв.get("size"), int):
                ряд.append(дв["size"])
            chk_ok = not дв.get("ok")
            if not chk_ok:
                причины.append("СОБРАЛОСЬ -- тогда этот модуль не нужен")
        chk(f"{метка}: двухшаговым путём не отправляется ни одна живая сделка",
            причины and all(ждём["pochemu"] in с for с in причины), причины[:2])
        if ждём["razmer"]:
            chk(f"{метка}: его пакет {ждём['razmer'][0]}...{ждём['razmer'][1]} байт "
                f"-- без нашей таблицы",
                ряд and (min(ряд), max(ряд)) == ждём["razmer"],
                (min(ряд), max(ряд)) if ряд else None)

    # ------------------- 7г. ПОЛУЧАТЕЛИ КОМИССИЙ: СПИСКИ, ВЫБОР, ПОДСТАНОВКА
    chk("три списка получателей -- по восемь, без дублей и без пересечений",
        len(POLUCHATELI_OBYCHNYE) == len(POLUCHATELI_MAYHEM)
        == len(POLUCHATELI_BUYBACK) == 8
        and len(set(POLUCHATELI_OBYCHNYE) | set(POLUCHATELI_MAYHEM)
                | set(POLUCHATELI_BUYBACK)) == 24,
        (len(POLUCHATELI_OBYCHNYE), len(POLUCHATELI_MAYHEM),
         len(POLUCHATELI_BUYBACK)))
    chk("наш выбор -- из списков программы",
        POLUCHATEL_NASH in POLUCHATELI_OBYCHNYE
        and BUYBACK_NASH in POLUCHATELI_BUYBACK
        and POLUCHATEL_NASH_MAYHEM in POLUCHATELI_MAYHEM, None)
    # СПИСКИ ИЗ ДОКУМЕНТОВ ПРОТИВ ЦЕПИ. Каждый получатель, стоявший на живых
    # v2-сделках, обязан быть в списке: иначе списки устарели, и выбирать по
    # ним нельзя. Сюда же идут НАША СОБСТВЕННАЯ покупка кривой
    # (data/krivaya_27_mesta.json) и живые продажи -- у них свои получатели.
    живые_об, живые_бб = set(), set()
    for о in все_кр:
        сч_о = о["tpl"]["accounts"]
        живые_об.add(сч_о[KRIVAYA_V2_MESTA_POLUCHATELEJ["poluchatel"]])
        живые_бб.add(сч_о[KRIVAYA_V2_MESTA_POLUCHATELEJ["buyback"]])
    п_пара = Path(C.DATA) / "krivaya_27_mesta.json"
    if п_пара.exists():
        д_п = json.loads(п_пара.read_text(encoding="utf-8"))
        for часть in ("покупка", "продажа"):
            сч_п = ((д_п.get(часть) or {}).get("accounts")
                    or (д_п.get(часть) or {}).get("счета") or [])
            if len(сч_п) == KRIVAYA_V2_SCHETOV:
                живые_об.add(сч_п[6])
                живые_бб.add(сч_п[8])
            elif len(сч_п) == 26:   # sell_v2: места те же 6 и 8
                живые_об.add(сч_п[6])
                живые_бб.add(сч_п[8])
    chk(f"все живые получатели комиссий ({len(живые_об)}) -- из списка обычных",
        живые_об and живые_об <= set(POLUCHATELI_OBYCHNYE),
        sorted(живые_об - set(POLUCHATELI_OBYCHNYE)))
    chk(f"все живые buyback-получатели ({len(живые_бб)}) -- из списка buyback",
        живые_бб and живые_бб <= set(POLUCHATELI_BUYBACK),
        sorted(живые_бб - set(POLUCHATELI_BUYBACK)))
    chk("их больше одного -- значит программа принимает ЛЮБОГО из списка, "
        "а не один фиксированный счёт",
        len(живые_об) >= 3 and len(живые_бб) >= 3,
        (len(живые_об), len(живые_бб)))
    if кр:
        о = кр[0]
        ст = storona(о["tx"], programma=PROG_KRIVAYA, hranilishche=о["vault"],
                     mint_kotirovki=о["quote"])
        сч = ст["tpl"]["accounts"]
        # НАША СБОРКА -- НАШИ ПОЛУЧАТЕЛИ; ПЕРЕСБОРКА ИСТОЧНИКА -- ЕГО.
        наш_ix = instrukciya_nogi(ст, user=KOSHELEK_PROVERKI, amount_in=1000,
                                  min_out=1, tx_istochnika=о["tx"])
        наши_сч = [str(м.pubkey) for м in наш_ix.accounts]
        P = KRIVAYA_V2_MESTA_POLUCHATELEJ
        chk("в НАШЕЙ покупке кривой стоят НАШИ получатели и их ATA",
            наши_сч[P["poluchatel"]] == POLUCHATEL_NASH
            and наши_сч[P["buyback"]] == BUYBACK_NASH
            and наши_сч[P["ata_poluchatelya"]]
            == B.ata(POLUCHATEL_NASH, сч[2], сч[4])
            and наши_сч[P["ata_buyback"]] == B.ata(BUYBACK_NASH, сч[2], сч[4]),
            [наши_сч[P[к]] for к in ("poluchatel", "ata_poluchatelya",
                                     "buyback", "ata_buyback")])
        u_ист = polzovatel_istochnika(ст, о["tx"])
        его_ix = instrukciya_nogi(ст, user=u_ист, amount_in=1000, min_out=1,
                                  tx_istochnika=о["tx"], kak_u_istochnika=True)
        его_сч = [str(м.pubkey) for м in его_ix.accounts]
        chk("а в ПЕРЕСБОРКЕ сделки источника -- его получатели (байт в байт цел)",
            его_сч == list(сч), [и for и, (x, y) in enumerate(zip(сч, его_сч))
                                 if x != y])
        # ПРОДАЖА: НАШИ МЕСТА И НАШИ ПОЛУЧАТЕЛИ
        import c3_prodavec_sborka as S_  # noqa: PLC0415
        ш_пр = S_.шаблон_продажи(о["tx"], программа=PROG_KRIVAYA,
                                 минт_базы=о["mint"],
                                 минт_котировки=о["quote"],
                                 хранилище=о["vault"])
        нм = nashi_mesta_prodazhi_v2(ш_пр, nash_koshelek=KOSHELEK_PROVERKI)
        chk("в продаже кривой подставлены ВСЕ девять наших мест",
            нм["ok"] and len(нм["podstavleno"]) == 9, нм.get("why_not"))
        if нм["ok"]:
            М = KRIVAYA_V2_MESTA_PRODAZHI
            нсч = нм["shablon"]["accounts"]
            chk("и это наш кошелёк, наши ATA, наш накопитель и наши получатели",
                нсч[М["user"]] == KOSHELEK_PROVERKI
                and нсч[М["poluchatel"]] == POLUCHATEL_NASH
                and нсч[М["buyback"]] == BUYBACK_NASH
                and нсч[М["uva"]] == B.pda([SEMYA_UVA, "USER"],
                                           KOSHELEK_PROVERKI, PROG_KRIVAYA),
                [нсч[М[к]] for к in ("user", "poluchatel", "buyback", "uva")])
            chk("у всех подставленных мест признак записи ВЫСТАВЛЕН (по IDL)",
                all(нм["shablon"]["writable"].get(а) for а
                    in нм["podstavleno"].values()),
                [а for а in нм["podstavleno"].values()
                 if not нм["shablon"]["writable"].get(а)])
            chk("места продажи сошлись с выводом по семенам IDL (чужой проверкой)",
                (S_.места_кривая_v2(нсч, минт_базы=о["mint"],
                                    наш_кошелёк=KOSHELEK_PROVERKI)
                 or {}).get("ok"),
                S_.места_кривая_v2(нсч, минт_базы=о["mint"],
                                   наш_кошелёк=KOSHELEK_PROVERKI).get("why_not"))
        # ПОЛУЧАТЕЛЬ ВНЕ СПИСКА -- ОТКАЗ ПО ИМЕНИ. Свой выбор подменяется на
        # время через модуль (не через global: объявление global после чтения
        # имени -- синтаксическая ошибка, и это ловится ещё при импорте).
        мод = sys.modules[__name__]
        сохр_пол = мод.POLUCHATEL_NASH
        try:
            мод.POLUCHATEL_NASH = DRUGOJ_KOSHELEK
            пл = mesta_krivoj_v2(сч, nash_koshelek=KOSHELEK_PROVERKI,
                                 nashi_poluchateli=True)
            chk("получатель вне списка программы -- отказ по имени, а не сборка",
                not пл["ok"]
                and WHY_POLUCHATEL_NE_V_SPISKE in (пл["why_not"] or ""),
                пл.get("why_not"))
        finally:
            мод.POLUCHATEL_NASH = сохр_пол

    # ------------------- 8а. СВЕРКА ПОРЯДКА РЕЗЕРВОВ ПО ВСЕМ КОТИРОВЩИКАМ
    # ЗАЧЕМ ЗАМЕР, А НЕ ЧТЕНИЕ КОДА. Все читатели резервов -- правило тонкого
    # пула (bloom_lane_two_step: thin pool и pool_reserve_quote_raw), наценка и
    # нижний предел резерва одношагового пути (bloom_own_send: pool_reserve_sol,
    # min_pool_sol_reserve) и мой c3_bilet_ot_rezerva -- берут ЭЛЕМЕНТ [0] КАК
    # КОТИРОВКУ. Значит порядок у каждого котировщика -- это денежное условие, и
    # он проверяется ЧИСЛАМИ на живых сделках: сравнением с остатками хранилищ
    # (какое из них котировочное, говорит mints_and_vaults).
    порядки = {}
    for p_, обр_ in образцы_по_типам.items():
        if not обр_:
            continue
        о_ = обр_[0]
        мо_ = B.min_out_from_reserves(о_["tpl"], о_["tx"], 1_000_000, 0.35)
        мо_ = мо_ if isinstance(мо_, dict) else {}
        пара = мо_.get("reserves_after") or мо_.get("virtual_reserves_after")
        мв_ = B.mints_and_vaults(о_["tpl"], о_["tx"]) or {}
        стр_ = {x["account"]: x for x in C.token_rows(о_["tx"]).values()}
        кот_ = стр_.get(мв_.get("quote_vault"), {}).get("post")
        баз_ = стр_.get(мв_.get("base_vault"), {}).get("post")
        порядки[p_] = {"пара": пара, "кот": кот_, "база": баз_,
                       "ключ": ("reserves_after" if мо_.get("reserves_after")
                                else "virtual_reserves_after"
                                if мо_.get("virtual_reserves_after") else None)}
    chk("CPMM и Pump AMM: reserves_after -- [КОТИРОВКА, база], как и ждут читатели",
        all(порядки[p_]["пара"] and порядки[p_]["пара"][0] == порядки[p_]["кот"]
            and порядки[p_]["пара"][1] == порядки[p_]["база"]
            for p_ in (PROG_CPMM, PROG_PUMP_AMM) if p_ in порядки),
        {TIPY[p_]["label"]: порядки[p_] for p_ in (PROG_CPMM, PROG_PUMP_AMM)
         if p_ in порядки})
    # LAUNCHLAB -- БЫЛ ЕДИНСТВЕННЫМ ОБРАТНЫМ (диф №5), И ДИФ ВЗЯТ 03.10.
    # Теперь проверка стоит НА ПРАВИЛЬНОМ порядке: разойдётся снова -- скажет.
    ев_лл = B.launchlab_event((образцы_по_типам.get(PROG_LAUNCHLAB) or [{}])[0]
                              .get("tx") or {}) or {}
    база_лл = int(ев_лл.get("virtual_base", 0)) - int(ев_лл.get("real_base_after", 0))
    кот_лл = int(ев_лл.get("virtual_quote", 0)) + int(ев_лл.get("real_quote_after", 0))
    chk("LaunchLab после дифа №5: virtual_reserves_after -- [КОТИРОВКА, база], "
        "как у двух соседей",
        (порядки.get(PROG_LAUNCHLAB, {}).get("пара") or [None, None])
        == [кот_лл, база_лл],
        порядки.get(PROG_LAUNCHLAB, {}).get("пара"))
    chk("и правило тонкого пула видит теперь КОТИРОВКУ: 4.18 SOL-экв. при "
        "пороге 30 -- пул тонкий, наценка срабатывает (было 5 527 021 и не "
        "срабатывала НИКОГДА)",
        база_лл > кот_лл * 1000
        and (порядки.get(PROG_LAUNCHLAB, {}).get("пара") or [None])[0] == кот_лл,
        (база_лл, кот_лл, порядки.get(PROG_LAUNCHLAB, {}).get("пара")))
    # КРИВАЯ -- [КОТИРОВКА (SOL), база]. Берётся сделка с НАТИВНОЙ котировкой:
    # у токеновой котировки чужой котировщик отказывает вовсе (обязательная часть
    # события пуста по SOL -- см. диф в разделе 10), и порядок там не проверить.
    пара_кр = None
    for r_ in B.load_samples(PROG_KRIVAYA):
        t_ = B.extract_template(r_["tx"], PROG_KRIVAYA, r_.get("pool_vault"))
        if not t_.get("ok"):
            continue
        м_ = B.min_out_from_reserves(t_, r_["tx"], 1_000_000, 0.35)
        if not isinstance(м_, dict) or not м_.get("ok"):
            continue
        ев_ = B.pump_trade_event(r_["tx"], r_.get("mint")) or {}
        пара_кр = (м_.get("virtual_reserves_after"),
                   [int(ев_.get("virtual_sol_reserves", -1)),
                    int(ев_.get("virtual_token_reserves", -1))])
        break
    chk("кривая pump.fun: virtual_reserves_after -- [КОТИРОВКА (SOL), база]",
        пара_кр and list(пара_кр[0] or []) == пара_кр[1], пара_кр)
    # ТИПЫ, КОТОРЫЕ РЕЗЕРВОВ НЕ ОТДАЮТ ВОВСЕ -- тоже замер, а не чтение: читатели
    # уходят в названную ветку «резерв неизвестен -- наценка обычная».
    B.загрузить_ставки_clmm()
    B.загрузить_ступени_dlmm()
    без_резервов = {}
    for p_, метка_ in ((PROG_CLMM_, "Raydium CLMM"), (PROG_DLMM_, "Meteora DLMM"),
                       (PROG_DAMM2_, "Meteora DAMM v2"), (PROG_DBC_, "Meteora DBC")):
        for r_ in B.load_samples(p_):
            t_ = B.extract_template(r_["tx"], p_, r_.get("pool_vault"))
            if not t_.get("ok"):
                continue
            м_ = B.min_out_from_reserves(t_, r_["tx"], 1_000_000, 0.35)
            if not isinstance(м_, dict) or not м_.get("ok"):
                continue
            без_резервов[метка_] = sorted(k for k in м_ if "reserves" in k)
            break
    chk("CLMM, DLMM, DAMM v2 и DBC резервов не отдают ВОВСЕ -- читатель уходит "
        "в ветку «резерв неизвестен»",
        len(без_резервов) == 4 and all(not v for v in без_резервов.values()),
        без_резервов)

    # ------------------------------------------------- 8б. тень
    # ТЕНЬ -- ЭТО ЧИСЛА БЕЗ ТРАНЗАКЦИИ, и вид ответа у неё обязан совпадать с
    # c2_usdc_noga.ten: врезка в полосу одна на две таблицы типов, и поля
    # журнала решений она берёт по именам.
    ключи_un = set(UN.ten(tx_istochnika={}, istochnik="", mint="", lamporty=1,
                          kesh_nog=None))
    os.environ[UN.FLAG] = UN.MODE_SHADOW
    os.environ.pop(UN.FLAG_GROUPS, None)
    тени = {}
    for p, обр in образцы_по_типам.items():
        if not обр:
            continue
        о = обр[0]
        кэш = SB.LegCache({}, None)
        кэш.entries[о["quote"]] = зап
        ист = о["source"] or (sorted(C.signers(о["tx"]))[0]
                              if C.signers(о["tx"]) else None)
        тени[p] = ten(tx_istochnika=о["tx"], istochnik=ист, mint=о["mint"],
                      lamporty=10_000_000, kesh_nog=кэш, proskalzyvanie=0.35,
                      mint_kotirovki=о["quote"])
    chk("тень даёт числа по всем четырём типам",
        len(тени) == 4 and all(т["ok"] and т["min_out"] > 0
                               for т in тени.values()),
        {TIPY[p]["label"]: (т["ok"], т.get("why_not")) for p, т in тени.items()})
    chk("и говорит, КАКОЙ котировщик дал число",
        {т["min_out_from"] for т in тени.values()}
        == {SPOSOB_REZERVY, SPOSOB_LAUNCHLAB, SPOSOB_KRIVAYA_V2},
        {TIPY[p]["label"]: т["min_out_from"] for p, т in тени.items()})
    chk("в тени нет ни транзакции, ни чтений сети",
        all("tx_base64" not in т and т["chtenij"] == 0 for т in тени.values()),
        None)
    нет_полей = sorted(ключи_un - set(next(iter(тени.values()))))
    chk("поля тени покрывают все поля c2_usdc_noga.ten -- врезка одна на две таблицы",
        not нет_полей, нет_полей)
    os.environ[UN.FLAG] = UN.MODE_OFF
    try:
        т_выкл = ten(tx_istochnika={}, istochnik="x", mint="y", lamporty=1,
                     kesh_nog=None)
        chk("флаг выключен -- тень тоже молчит и называет причину",
            not т_выкл["ok"] and т_выкл["why_not"] == UN.WHY_OFF,
            т_выкл.get("why_not"))
    finally:
        for k, v in сохр.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ------------------------------------------------- 9. режимы
    os.environ[UN.FLAG] = UN.MODE_OFF
    try:
        сб = sobrat(tx_istochnika={}, istochnik="x", mint="y",
                    nash_koshelek=KOSHELEK_PROVERKI, lamporty=1, kesh_nog=None)
        chk("флаг выключен -- сборки нет и причина названа",
            not сб["ok"] and сб["why_not"] == UN.WHY_OFF, сб.get("why_not"))
        os.environ[UN.FLAG] = UN.MODE_SHADOW
        сб = sobrat(tx_istochnika={}, istochnik="x", mint="y",
                    nash_koshelek=KOSHELEK_PROVERKI, lamporty=1, kesh_nog=None)
        chk("режим тени -- транзакция на отправку не собирается",
            not сб["ok"] and сб["why_not"] == UN.WHY_SHADOW, сб.get("why_not"))
    finally:
        for k, v in сохр.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    рез = instrukcii(tx_istochnika={}, istochnik="x", mint="y",
                     nash_koshelek=KOSHELEK_PROVERKI, lamporty=1, kesh_nog=None)
    chk("без кэша первой ноги -- отказ словами, а не исключение",
        not рез["ok"] and "кэша шаблонов" in (рез["why_not"] or ""),
        рез.get("why_not"))

    print(f"\nпроверок {было}, ждали {ZHDEM_PROVEROK}, не прошло {плохо}")
    if было != ZHDEM_PROVEROK:
        print(" ПЛОХО число проверок разошлось с объявленным -- "
              "молчаливый пропуск считается провалом")
        return 1
    return 1 if плохо else 0


def main() -> int:
    import argparse  # noqa: PLC0415
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--self-test", action="store_true", default=True)
    p.add_argument("--zamer", metavar="ФАЙЛ", default=None,
                   help="записать таблицу замера по типам в файл")
    a = p.parse_args()
    код = self_test()
    if a.zamer:
        put_ = Path(a.zamer)
        put_.parent.mkdir(parents=True, exist_ok=True)
        put_.write_text(json.dumps({
            "что": "USDC-нога для сигнальных типов: что проверено числами",
            "типы": {TIPY[p_]["label"]: {"программа": p_,
                                        "сигналов_в_сутки_Code1": SIGNALOV_V_SUTKI[p_],
                                        "котировщик": TIPY[p_]["kotirovshchik"],
                                        **ZHDEM_PO_TIPAM[p_],
                                        "размер": ZHDEM_RAZMEROV[p_]}
                      for p_ in TIPY},
            "байт_в_байт_всего": ZHDEM_BAJT_VSEGO,
            "продажа_токен_USDC_SOL": {
                TIPY[p_]["label"]: ZHDEM_PRODAZHI[p_] for p_ in TIPY},
            "кривая_лестница_добавок": ZHDEM_KRIVAYA_LESTNICA,
            "кривая_набор_для_таблицы": ZHDEM_NABORA,
            "продажа_ноги_2": {
                "как": "зеркало нашей покупки первой ноги без мест 19 и 20",
                "счетов": 23,
                "живым_образцом_не_проверена": True,
                "запрошено_у_Code2": "6+ живых продаж Pump AMM с 23 счетами"},
            "таблица_адресов": {"ключ": TABLICA_ADRESOV, "адресов": ADRESOV_V_TABLICE,
                                "кривой_не_хватает": "1249 байт при пределе 1232",
                                "лекарство": ZHDEM_KRIVAYA_LESTNICA},
            "образец_кривой": OBRAZEC_KRIVOJ,
            "двухшаговым_путём_сейчас": {TIPY[p_]["label"]: ZHDEM_DVUHSHAGOVYJ[p_]
                                         for p_ in TIPY},
            "CU": {"предел_ноги_по_умолчанию": _moduli()[2].CU_NOGI_PO_UMOLCHANIYU,
                   "переменная": _moduli()[2].IMYA_FLAGA_CU,
                   "замер_Code1_две_ноги": list(_moduli()[2].CU_ZAMER_CODE1),
                   "здесь_не_мерено": (
                       "simulateTransaction не мой инструмент; у живых образцов в CU "
                       "входит весь чужой маршрут (88 869...541 083 CU на пакет "
                       "источника), чистых сделок «только своп» по этим четырём "
                       "типам в образцах нет")},
            "живых_USDC_инструкций": {TIPY[p_]["label"]: ZHDEM_USDC_ZHIVYH[p_]
                                      for p_ in TIPY},
            "образец_USDC_Pump_AMM": OBRAZEC_USDC_PUMP_AMM,
            "запрошено_у_Code2": "по 6+ живых USDC-свопов на каждый тип",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"записано {put_}")
    return код


if __name__ == "__main__":
    raise SystemExit(main())
