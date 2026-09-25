# Ночь 25.09: боевое и исследование (доклад собран 2026-09-25T01:52:55Z)

## 1. Боевое

**Полоса своей отправки.** Путей 0, отправлено 0, отказ по симуляции (SKIP_SIM_FAIL) 0. Позиций полосы 0, закрыто 0, UNSOLD 0. Итог по закрытым: — (по 0 сделкам). Медиана от отправки до появления в потоке: —.

**Пары «Bloom против нашей».** Пар 0, из них мы раньше 0; медиана разницы — (плюс -- мы раньше).

**Тень.** Записей 8, по вердиктам: {"would_pass": 3, "нет вердикта": 5}. Медиана сборки 8.44 мс, симуляции 13.7 мс.

**Узкий фильтр по налогу маршрута.** Пропусков 0, тень измерила 0: цена через 28.8 с была ниже входа в 0 случаях (фильтр сберёг), выше -- в 0 (фильтр отнял), неизвестна в 0. Оценка сбережённого при размере 0.2 SOL: —.

**Разложение закрытых сделок с 2026-09-24T00:00:00Z.** Сделок 25, сумма итога 0.039993785 SOL, медиана -0.0 SOL. По каждой сделке в JSON: налог по маршруту в SOL, комиссия Bloom, приоритет и чаевые, остаток (ход цены).

## P1 (тень против Bloom)
```json
{
  "ok": true,
  "built_utc": "2026-09-25T01:52:54Z",
  "pickaxe": {
    "ok": true,
    "client_order_id": "7b11a2d6f2284e50a6d9e275f9e2b824",
    "mint": "6QxMcEpYULAUs4Qa28ui2GJ55daY2KqFLRJXHEosNPAu",
    "ts_intent_utc": "2026-09-25T00:31:53Z",
    "source_sig": "2Nm7Ef1QUsoAZ8AUvC4d34oNbtEuZqbLfL8vy8ihVSv9dRyPZbz4umEsY4TV9r1FDTbw4cdrapKs7pyP7Vj3qtcA",
    "bloom_signature": "2Vs5qv9V2HVhbNzd5sMzgfqLFZ37SxbbMDUBjm6a6PaFkX6N6m8BGgzEePAp4uvdiwQvV8E2UaCorAktn2Lo5fCS",
    "chain_ok": true,
    "bloom_route": {
      "programs": "Raydium AMM v4,Orca Whirlpool,Raydium CPMM",
      "direct": false,
      "hops_by_mints": 1
    },
    "collected": false,
    "why_not": "котировка не SOL: нужен второй шаг (кэш шаблонов не передан)",
    "shadow_route": "one_hop",
    "shadow_pool_program": "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
    "shadow_pool_label": "Raydium CPMM",
    "shadow_sim_verdict": null,
    "shadow_build_ms": null,
    "shadow_expected_out_raw": null,
    "shadow_min_out_raw": null,
    "shadow_why_not": "котировка не SOL: нужен второй шаг (кэш шаблонов не передан)",
    "route_agrees_with_bloom": null,
    "bloom_bought_raw": null,
    "diff_pct": null,
    "verdict_ours": null,
    "pct_note": "тень посчитана без комиссии Bloom и без налога токена на перевод -- Bloom bought_raw их уже несёт, поэтому diff_pct показывает разницу С УЧЁТОМ них, а не чистую разницу цены пула"
  },
  "summary": {
    "since": "2026-09-24T00:00:00Z",
    "state_dir": "/tmp/night_state",
    "positions_why_not": null,
    "shadow_why_not": null,
    "excluded_not_bloom_lane_or_dry": 0,
    "excluded_before_since": 1,
    "total_bloom_trades": 30,
    "collected": 2,
    "collected_share": 0.0667,
    "with_diff_pct": 0,
    "median_diff_pct": null
  },
  "rows": [
    {
      "client_order_id": "a6ef989063f7425aa670b0d5ec8bcbf2",
      "mint": "KMNo3nJsBXfcpJTVhZcXLW7RmTwTt4GVFE7suUBo9sS",
      "ts_intent_utc": "2026-09-24T00:07:05Z",
      "source_sig": "23SefhJsSQ79XfNSYzSzjptCZQSZNXyVtp4ZR7E5aWKmPEJCewrfyBGCZFRwYU8yxJC2GFaZujNtD6DsSFLMCqZh",
      "bloom_signature": "4jPfUpVAWTd46yFaVHqKVxLkojPPA4YV7UN9JHaK2eh4HoFReZMUAbKzoyqY55TRkJTbBNnkEiFp5tNxyinxAbPE",
      "chain_ok": true,
      "bloom_route": {
        "programs": "Raydium AMM v4,Orca Whirlpool",
        "direct": false,
        "hops_by_mints": 1
      },
      "collected": false,
      "why_not": "тени нет: в decisions.jsonl нет строки stage=shadow по подписи источника этой покупки",
      "shadow_route": null,
      "shadow_pool_program": null,
      "shadow_pool_label": null,
      "shadow_sim_verdict": null,
      "shadow_build_ms": null,
      "shadow_expected_out_raw": null,
      "shadow_min_out_raw": null,
      "shadow_why_not": null,
      "route_agrees_with_bloom": null,
      "bloom_bought_raw": null,
      "diff_pct": null,
      "verdict_ours": null,
      "pct_note": "тень посчитана без комиссии Bloom и без налога токена на перевод -- Bloom bought_raw их уже несёт, поэтому diff_pct показывает разницу С УЧЁТОМ них, а не чистую разницу цены пула"
    },
    {
      "client_order_id": "27b4f0ca2d604288a2d597aa3c84bf28",
      "mint": "4nV5gNwwP68zUDat26ySChREqVaQaLudfJBkSgEzpump",
      "ts_intent_utc": "2026-09-24T00:21:06Z",
      "source_sig": "2r7o2xMzvhQKDT2EABSsCvUqF46jHCB94RfLHYViU3XRkKkWyKyGPjRvSE8jM6ETk4kTdJJnjGKggncEr2NUtga",
      "bloom_signature": "5Pf6CNG64mBCDdTyB8ZkhSpZ6fk64TU3xwxpnmBPayHh5KEAeRZbUVHq5FELy76532jygM6XHxGY2WsWQ4JyNpHV",
      "chain_ok": true,
      "bloom_route": {
        "programs": "Pump AMM",
        "direct": true,
        "hops_by_mints": 1
      },
      "collected": false,
      "why_not": "тени нет: в decisions.jsonl нет строки stage=shadow по подписи источника этой покупки",
      "shadow_route": null,
      "shadow_pool_program": null,
      "shadow_pool_label": null,
      "shadow_sim_verdict": null,
      "shadow_build_ms": null,
      "shadow_expected_out_raw": null,
      "shadow_min_out_raw": null,
      "sha
```

## P2 (сверка по цепи)
```json
{
  "ok": true,
  "built_utc": "2026-09-25T01:52:55Z",
  "credits_used": 3,
  "credits_by_method": {
    "getBlock": 3
  },
  "slots": [
    450191590,
    450191591,
    450191592
  ],
  "read_method": {
    "getBlock_credits": 3,
    "getTransactions_credits": 5,
    "chosen": "getBlock"
  },
  "blocks": {
    "450191590": {
      "known": true,
      "total": 1083,
      "why_not": null
    },
    "450191591": {
      "known": true,
      "total": 1150,
      "why_not": null
    },
    "450191592": {
      "known": true,
      "total": 1687,
      "why_not": null
    }
  },
  "signatures": {
    "leader": {
      "signature": "2Nm7Ef1QUsoAZ8AUvC4d34oNbtEuZqbLfL8vy8ihVSv9dRyPZbz4umEsY4TV9r1FDTbw4cdrapKs7pyP7Vj3qtcA",
      "position": {
        "slot": 450191590,
        "index": 935,
        "total": 1083
      },
      "why_not": null,
      "compute_unit_price_micro": 273608,
      "compute_unit_limit": 428032,
      "price_source": "raw_decode",
      "limit_source": "raw_decode",
      "priority_fee_lamports": 117113
    },
    "sniper1": {
      "signature": "2At3Drxm5xjwWHfFEEYid7S8dTu3T2V2JBuFFhsG3ywV46XskhXw9XpRpmY2S9xKdzdJN9qidY6ka9vEyQqd43w5",
      "position": {
        "slot": 450191590,
        "index": 1032,
        "total": 1083
      },
      "why_not": null,
      "compute_unit_price_micro": 96764,
      "compute_unit_limit": 103344,
      "price_source": "raw_decode",
      "limit_source": "raw_decode",
      "priority_fee_lamports": 10000
    },
    "sniper2": {
      "signature": "4JK7Qg638rYNMeNiTqzaLYCT6qLdPC2r8oZXgGQUaLetoi5tEtbRYaSq9YkgAGx1ZCuDChGk98KMw6QC8RxGU5Li",
      "position": {
        "slot": 450191590,
        "index": 1062,
        "total": 1083
      },
      "why_not": null,
      "compute_unit_price_micro": 41095890,
      "compute_unit_limit": 730000,
      "price_source": "raw_decode",
      "limit_source": "raw_decode",
      "priority_fee_lamports": 30000000
    },
    "ours": {
      "signature": "2Vs5qv9V2HVhbNzd5sMzgfqLFZ37SxbbMDUBjm6a6PaFkX6N6m8BGgzEePAp4uvdiwQvV8E2UaCorAktn2Lo5fCS",
      "position": {
        "slot": 450191591,
        "index": 688,
        "total": 1150
      },
      "why_not": null,
      "compute_unit_price_micro": 2299769,
      "compute_unit_limit": 432652,
      "price_source": "raw_decode",
      "limit_source": "raw_decode",
      "priority_fee_lamports": 995000
    },
    "dbot": {
      "signature": "QhKVtVuZDCgNMahCcWpBoHkQySaPfjk3giFArTL2j5jQWk3LAK66za1Ciex98euH32BAmrL52uhvEovVESUs1WR",
      "position": {
        "slot": 450191592,
        "index": 1359,
        "total": 1687
      },
      "why_not": null,
      "compute_unit_price_micro": 1568810,
      "compute_unit_limit": 325087,
      "price_source": "raw_decode",
      "limit_source": "raw_decode",
      "priority_fee_lamports": 510000
    }
  },
  "transactions_between_leader_and": {
    "sniper1": {
      "known": true,
      "count": 96
    },
    "sniper2": {
      "known": true,
      "count": 126
    },
    "ours": {
      "known": true,
      "count": 835
    },
    "dbot": {
      "known": true,
      "count": 2656
    }
  },
  "tip_accounts_check": {
    "are_sender_max_tip_accounts": true,
    "found_in": "analysis/bloom_own_send.py:TIP_ACCOUNTS",
    "confirmed_on_saved_doc_page": true,
    "doc_page": "/home/ghrunner/actions-runner/_work/robinhood-chain-alpha/robinhood-chain-alpha/data/docs/helius_sender_max.md"
  },
  "tip_transfers_found_in_blocks": [
    {
      "slot": 450191590,
      "index": 364,
      "signature": "wEMsUewxM2yKijnLdoW5W8dsUSxbH2TrH1cpARG2GozuSb3wNipCKCE93PAaCeE8NDX3VJ88GTwrcxnmXpsA5Te",
      "tip_account": "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
      "source": "BuekUk3YMmm7Agnb4ni1qMCmShVpeBTdQZYNyihyzr7u",
      "lamports": 2025404,
      "sol": 0.002025404
    },
    {
      "slot": 450191590,
      "index": 371,
      "signature": "48ho7vH8wJ4HL7in4fvJK3qssjHo9YSFB4VkBih1xJkEqr798GEQiGh1a72hj9tnFmTLKELLRa3LTZ7q4XZMiQr5",
      "tip_account"
```

## A/D (есть ли деньги, правила)
```json
{
  "schema_version": 1,
  "generated_utc": "2026-09-25T01:49:05Z",
  "elapsed_s": 0.85,
  "входные_файлы": {
    "crowd": "/tmp/claude-0/-home-user-robinhood-chain-alpha/1766891f-706e-5008-a349-9de1fb730381/scratchpad/c2/crowd_metric_2026-09-24.json",
    "tax_groups": "data/solana_tax_groups.json",
    "transfer_fee_audit": "data/solana_transfer_fee_audit.json",
    "leg_pools": "/tmp/claude-0/-home-user-robinhood-chain-alpha/1766891f-706e-5008-a349-9de1fb730381/scratchpad/c2/c2_leg_pools_2026-09-24.json"
  },
  "окно": {
    "window_from_utc": "2026-09-17T16:02:53Z",
    "window_to_utc": "2026-09-24T16:02:53Z",
    "n_sources": 18,
    "n_trades_raw": 346,
    "generated_utc": "2026-09-24T16:24:26Z"
  },
  "n_сигналов_всего": 346,
  "n_с_известным_net_на_28_8с": 132,
  "n_с_известным_net_на_60с": 133,
  "ЧЕСТНЫЕ_ОГОВОРКИ": [
    "Входы (i)/(ii)/(iii) численно совпадают: единственная реальная цена после источника в кэше -- spot_after; цены на границе слотов S+1/S+2 нет, интерполяция запрещена условием задачи. См. докстринг модуля.",
    "Таймер 28.8с считается по ближайшей реальной точке 30с (growth_after_30s), без интерполяции; разница ~1.2с (~4% таймера) объявлена, не скрыта.",
    "Горизонты 10с/5мин/до первой продажи источника отсутствуют в кэше целиком и везде помечены missing/why_not -- не 0 и не оценка.",
    "Комиссия пула (swap fee AMM) НЕ включена в net_sol: в кэше и репозитории нет ставки bps по pool_program, только ярлыки программ. Реальный чистый результат ниже посчитанного на эту неизвестную величину.",
    "Налоговый статус минта 'неизвестно' (минта нет в solana_transfer_fee_audit.json) НЕ считается 'без налога' -- такие сделки помечены missing и не входят в среднее/сумму/долю в плюс (214 из 346 сделок без полного net на горизонте 28.8с).",
    "'Возраст токена' и 'до первой продажи источника' не оцениваются вообще: ни в одном переданном файле нет времени создания минта или времени продажи источника.",
    "'Число шагов маршрута' и 'глубина пула' в правилах D -- ПРОКСИ по имеющимся полям (split_route, глубина пула КОТИРОВОЧНОГО минта к SOL, а не пула самого таргет-минта), не измеренные величины; см. hops_proxy/leg_pool_sol_depth в докстрингах."
  ],
  "A3": {
    "t28_8": {
      "i_сразу_за_источником": {
        "все": {
          "сделок": 132,
          "сделок_без_цифры": 214,
          "среднее_sol": 0.067482,
          "медиана_sol": 0.025521,
          "доля_в_плюс": 0.7273,
          "сумма_sol": 8.907579
        },
        "бутстрэп_среднего": {
          "сделок": 132,
          "среднее_sol": 0.067482,
          "интервал_95_низ_sol": 0.046358,
          "интервал_95_верх_sol": 0.090761,
          "выборок": 2000,
          "сид": 20260925,
          "ноль_внутри_интервала": false
        },
        "вывод": "край есть",
        "без_3_лучших": {
          "сделок": 129,
          "сделок_без_цифры": 0,
          "среднее_sol": 0.052179,
          "медиана_sol": 0.022964,
          "доля_в_плюс": 0.7209,
          "сумма_sol": 6.73115
        },
        "без_5_лучших": {
          "сделок": 127,
          "сделок_без_цифры": 0,
          "среднее_sol": 0.047588,
          "медиана_sol": 0.022256,
          "доля_в_плюс": 0.7165,
          "сумма_sol": 6.043685
        },
        "первая_половина_окна": {
          "сделок": 62,
          "сделок_без_цифры": 139,
          "среднее_sol": 0.086031,
          "медиана_sol": 0.046656,
          "доля_в_плюс": 0.8226,
          "сумма_sol": 5.333942
        },
        "первая_половина_бутстрэп": {
          "сделок": 62,
          "среднее_sol": 0.086031,
          "интервал_95_низ_sol": 0.053688,
          "интервал_95_верх_sol": 0.125186,
          "выборок": 2000,
          "сид": 20260925,
          "ноль_внутри_интервала": false
        },
        "вторая_половина_окна": {
          "сделок": 70,
          "сделок_без_цифры": 75,
          "среднее_sol": 0.051052,
          "медиана_sol": 0.015786,
          "доля_в_плюс": 0.6429,
          "сумма_
```

## B/C (токсичность, манипуляции)
```json
{
  "schema_version": 1,
  "generated_utc": "2026-09-25T01:50:32Z",
  "inputs": {
    "crowd_file": "/tmp/claude-0/-home-user-robinhood-chain-alpha/1766891f-706e-5008-a349-9de1fb730381/scratchpad/c2/crowd_metric_2026-09-24.json",
    "followers_file": "/tmp/claude-0/-home-user-robinhood-chain-alpha/1766891f-706e-5008-a349-9de1fb730381/scratchpad/c2/c2_block_position_2026-09-24.json",
    "tax_catalog_file": "/home/user/robinhood-chain-alpha/data/solana_transfer_fee_audit.json",
    "tax_groups_file": "/home/user/robinhood-chain-alpha/data/solana_tax_groups.json",
    "n_trades": 346,
    "n_unique_mints": 273,
    "n_unique_pools": 285,
    "n_sources": 18
  },
  "task_b_toxic_features": {
    "coverage": {
      "growth_30s_known": 311,
      "growth_30s_total": 346,
      "tax_known_from_catalog": 186,
      "tax_catalog_size": 338
    },
    "feature_catalog": [
      {
        "name": "quote_kind",
        "source": "cache",
        "field": "quote_mint"
      },
      {
        "name": "pool_kind",
        "source": "cache",
        "field": "pool_program"
      },
      {
        "name": "split_route",
        "source": "cache",
        "field": "split_route"
      },
      {
        "name": "failed_same_block",
        "source": "cache",
        "field": "failed_same_block_wallets"
      },
      {
        "name": "impact_source (суррогат доли глубины)",
        "source": "cache_partial",
        "field": "impact_source",
        "n_known": 250
      },
      {
        "name": "spend_size",
        "source": "cache",
        "field": "spend_sol_equiv"
      },
      {
        "name": "crowd_30s (суррогат толпы в слоте)",
        "source": "cache_partial",
        "field": "crowd_30s",
        "n_known": 274
      },
      {
        "name": "taxable (налог самого токена)",
        "source": "cache_partial",
        "field": "data/solana_transfer_fee_audit.json:минты",
        "n_known": 186
      },
      {
        "name": "tax_max_fee",
        "source": "cache_partial_or_chain",
        "note": "потолок есть в том же каталоге для тех же n_tax_known минтов"
      },
      {
        "name": "route_tax_bps (налог по маршруту)",
        "source": "chain_required",
        "rpc": [
          "getTransaction",
          "getAccountInfo"
        ]
      },
      {
        "name": "tax_authority_revoked",
        "source": "chain_required",
        "rpc": [
          "getAccountInfo"
        ]
      },
      {
        "name": "mint_authority",
        "source": "chain_required",
        "rpc": [
          "getAccountInfo"
        ]
      },
      {
        "name": "freeze_authority",
        "source": "chain_required",
        "rpc": [
          "getAccountInfo"
        ]
      },
      {
        "name": "route_hops (число шагов маршрута)",
        "source": "chain_required",
        "rpc": [
          "getTransaction"
        ]
      },
      {
        "name": "pool_depth_sol_equiv (глубина пула, абсолют)",
        "source": "chain_required",
        "rpc": [
          "getTransaction"
        ]
      },
      {
        "name": "top10_holder_share",
        "source": "chain_required",
        "rpc": [
          "getTokenLargestAccounts",
          "getAccountInfo (supply)"
        ]
      },
      {
        "name": "token_age / pool_age",
        "source": "chain_required",
        "rpc": [
          "getSignaturesForAddress"
        ]
      },
      {
        "name": "bundle_launch (несколько кошельков в первом слоте)",
        "source": "chain_required",
        "rpc": [
          "getSignaturesForAddress",
          "getBlock"
        ]
      },
      {
        "name": "slot_crowd_S_and_S+1",
        "source": "chain_required",
        "rpc": [
          "getBlock",
          "getBlock"
        ]
      }
    ],
    "credit_cost_full_chain_pass": {
      "n_trades": 346,
      "n_unique_mints": 273,
      "n_unique_pools": 285,
      "age_max_pages": 5,
      "rows_credits": {
        "getTransaction_refetch_route_and_exact
```

## E/F/G (лидер, GP, StonkFun)
НЕ СОБРАНО: файла нет: night_leader.json

## Что из данных НЕ следует

Прочерк в таблице -- это не ноль: там, где стоит «—», величина не измерена, и подставлять вместо неё ноль нельзя. Оценка сбережённого фильтром считается по цене пула через 28.8 с и при размере сделки 0.2 SOL: это модель одного горизонта, а не факт нашей сделки, которой не было. Разница пары «Bloom против нашей» измерена по времени появления транзакций в подписке processed нашего узла; это наш узел, а не общее время сети.
