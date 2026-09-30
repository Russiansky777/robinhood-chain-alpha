# Гонка отправителей: модуль и диф Code-1

**Файл:** `analysis/c2_zabor_gonka.py`. **Самопроверка:** `python3
analysis/c2_zabor_gonka.py --self-test` — **39/39**, без сети. Ничего не запускал.

## Сначала число, которое решает, будет ли прогон вообще

Гонка тремя отправителями стоит **за раунд**: `5 000 + 200 000` (helius)
`+ 5 000 + 1 000 000` (astralane) `+ 5 000 + 1 000 000` (triton) =
**2 215 000 лампортов = 0.002215 SOL**.

**Тридцать раундов — 0.06645 SOL.** Потолок в модуле — твой,
`ПОТОЛОК_РАСХОДА_SOL = 0.005` (слово владельца 30.09 16:40Z). Тридцать раундов
**в тринадцать раз выше потолка**; в потолок влезают **два раунда**.

Модуль считает это **до первой отправки** и отказывает по имени
(`цена гонки выше потолка расхода`), называя цену, потолок и сколько влезает. Молча
урезать раунды он не будет: «30 раундов» в отчёте, где их было два, — это ложь о
выборке. Решение владельца: поднять потолок до ~0.07 SOL или взять меньше раундов.
Проверка на это есть: «30 раундов: отказ ДО первой отправки, ни одной отправки не
было».

Чаевые берутся **твоей** функцией `c2_zabor_s0_opyt.чаевые_отправителя`: helius
200 000 и astralane 1 000 000 — по их собственным отказам, как ты и записал;
triton своего числа не называл, поэтому идёт `bloom_senders.минимум_чаевых` =
1 000 000 (в реестре `min_tip_lamports` у него не задан). Своих чисел в модуле нет.

## Почему отдельный файл, а не правка твоего

`c2_zabor_s0_opyt.py` ты правил 30.09 на 868 строк. Правило владельца — файлы,
которые правил Code-1, не переписывать, диф ему. Всё общее берётся **импортом**:
кошелёк опыта, `кошелёк_проверен`, `транзакция_самоперевода`, `подписать_вариант`,
`посадка`, `чаевые_отправителя`, `КОМИССИЯ_ЛАМПОРТЫ`, `ПОТОЛОК_РАСХОДА_SOL`,
`_результат`. Ни одна из этих вещей не продублирована: разойдись они, деньги ушли
бы по одной ветке, а считались по другой.

## Что меряет один раунд

По одной транзакции **каждому** отправителю, самоперевод 0.00001 SOL, чаевые —
минимум этого отправителя, **blockhash один на всех**, подписать все, отправить
**одним барьером** (иначе ушедший первым получает фору в слот). Через 30 с по цепи:
слот посадки, **индекс в блоке** (`getBlock` с `transactionDetails=signatures` —
тот же запрос, что у `bloom_timing_breakdown.позиция_в_блоке`), **регион лидера**
(`bloom_region_send.регион_слота`), **комиссия плюс чаевые**. Пауза — **не меньше
двух слотов**, и ждём номер слота, а не время: при заторе слот стоит дольше 400 мс,
и пауза «0.8 с» оставила бы два раунда в одном слоте.

Итог — таблица по отправителю: чаевые, отправок, село / не село, медиана «слот
посадки − слот отправки», медиана индекса, регионы лидера. **Победитель называется
только при равном числе отправок** — сравнивать медианы у того, кто отправил 30 раз,
и у того, кто 3, это не гонка; проверка на это есть.

## Диф 1: CLI в `analysis/c2_zabor_s0_opyt.py` (три места)

```python
# (1) в _разборщик(), в choices режима:
    р.add_argument("--rezhim", default="kolco",
                   choices=("kolco", "opyt", "gonka"),
                   help="kolco -- только кольцо; opyt -- три варианта одним "
                        "отправителем; gonka -- по одной каждому отправителю")

# (2) там же, два новых входа (оба только для gonka):
    р.add_argument("--otpraviteli", default="helius,astralane,triton",
                   help="гонка: отправители через запятую")
    р.add_argument("--pauza-slotov", type=int, default=2,
                   help="гонка: пауза между раундами в слотах")

# (3) в main(), СРАЗУ ПОСЛЕ получения `зов` и ДО сборки кольца
#     (кольцо гонке не нужно: у неё один blockhash на всех):
    if а.rezhim == "gonka":
        import c2_zabor_gonka as G  # noqa: PLC0415

        о = G.запустить(зов, раундов=а.raundov,
                         отправители=[и.strip() for и in
                                      str(а.otpraviteli).split(",") if и.strip()],
                         ждать_с=а.zhdat, пауза_слотов_=а.pauza_slotov)
        G.печать(о)
        _записать(о, а.out)
        return 0 if (о.get("строки") and not о.get("why_not")) else 2
```

Больше в твоём файле ничего менять не нужно: рубильник, проверка кошелька и потолок
расхода у гонки **твои же**.

## Диф 2: `deploy/zabor/na_hoste.sh` (только ASCII, как и весь файл)

```sh
# (1) razreshit rezhim gonka -- v case REZHIM:
case "$REZHIM" in
  kolco|opyt|gonka) ;;
  *) echo "SBOY: rezhim ne kolco, ne opyt i ne gonka: $REZHIM"; exit 1;;
esac

# (2) otpraviteli -- tolko dlja gonki, s proverkoj kazhdogo imeni:
if [ "$REZHIM" = gonka ]; then
  : "${OTPRAVITELI:?SBOY: OTPRAVITELI ne zadany}"
  for O in $(echo "$OTPRAVITELI" | tr ',' ' '); do
    case "$O" in
      helius|astralane|triton) ;;
      *) echo "SBOY: neizvestnyj otpravitel v spiske: $O"; exit 1;;
    esac
  done
fi

# (3) sekret nuzhen i gonke -- v uslovii chtenija kljucha:
if [ "$REZHIM" = opyt ] || [ "$REZHIM" = gonka ]; then

# (4) v samom zapuske -- dopolnitelnyj argument tolko dlja gonki:
  --rezhim "$REZHIM" \
  --otpravitel "$OTPRAVITEL" \
  --otpraviteli "${OTPRAVITELI:-helius,astralane,triton}" \
  --raundov "$RAUNDOV" \
```

`--otpraviteli` безвреден в режимах `kolco` и `opyt`: они его не читают.

## Диф 3: прогон `run_zabor_s0_opyt_nl.yml`

```yaml
# (1) v rezhim dobavit gonka:
      rezhim:
        type: choice
        default: kolco
        options: [kolco, opyt, gonka]

# (2) novyj vhod:
      otpraviteli:
        description: "gonka: otpraviteli cherez zapjatuju"
        required: false
        default: "helius,astralane,triton"

# (3) podtverzhdenie -- to zhe slovo i dlja gonki (ona tozhe tratit SOL):
          if { [ "$REZHIM" = opyt ] || [ "$REZHIM" = gonka ]; } \
             && [ "$PODTV" != otpravlyat ]; then

# (4) v shage dostavki -- peredat spisok v fajl parametrov:
        env:
          OTPRAVITELI: ${{ inputs.otpraviteli }}
          ...
          printf 'OTPRAVITEL=%s\nRAUNDOV=%s\nREZHIM=%s\nOTPRAVITELI=%s\n' \
            "$OTPRAVITEL" "$RAUNDOV" "$REZHIM" "$OTPRAVITELI" > /tmp/zabor_params

# (5) v shage sekreta -- uslovie i dlja gonki:
        if: ${{ inputs.rezhim == 'opyt' || inputs.rezhim == 'gonka' }}
```

Двойных кавычек внутри `ssh` ни одна из этих правок не добавляет: всё идёт через
файл параметров и `bash -s < файл`, как сейчас.

## Страница итога

`deploy/checks/zabor_s0_stranica.py` менять **не нужно**: он берёт `о["таблица"]`
как есть, а гонка кладёт туда свою. В заголовке страницы будет «режим gonka» — если
хочешь другое слово, это одна строка у тебя.

## Чего в этом нет

Прогонов не запускал, на хост не ходил, сети не трогал. Живых чисел гонки у меня
нет и быть не может — это твой шаг. Всё выше проверено офлайн на подставном узле и
подставном отправителе: 39 из 39.
