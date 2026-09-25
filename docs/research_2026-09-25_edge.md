# Ночь 25.09: боевое и исследование (доклад собран 2026-09-25T05:24:40Z)

## 1. Боевое

**Решения по сигналам.** Покупок 10; по кодам: {"NOT_A_BUY": 206, "SKIP_TARGET_INCREASE_POSITION": 107, "TARGET_AMOUNT_OUT_OF_RANGE": 105, "TOKEN_RECEIVED_NOT_BOUGHT": 96, "BUY": 10, "SKIPPED_DUP_MINT": 2, "THRESHOLD_EDGE": 1, "SKIP_TAXED_ROUTE": 1}.

**Полоса своей отправки.** Путей 5, отправлено 1, отказ по симуляции (SKIP_SIM_FAIL) 0. Позиций полосы 1, закрыто 1, UNSOLD 0. Итог по закрытым: — (по 0 сделкам). Медиана от отправки до появления в потоке: 781.09 мс.
**ПОЛОСА ОСТАНОВЛЕНА РУБИЛЬНИКОМ.** полоса остановлена: 2026-09-25T05:18:48Z raskhozhdenie ucheta ostatka: polosa kupila 3453513816 raw minta 571aBZbC, a k prodazhe ostatok minta byl 0 -- avtoprodazha Bloom cherez 28.8 s prodala ves balans koshelka vmeste s nashimi tokenami; itog polosy poschitat nechem. Bez vladeltsa ne chinit. Снимать -- только владельцу; Bloom, сторож продаж и тень работают.
Где путь останавливался: build -- 3, sent -- 1, lane_seen -- 1. Причины: котировка пула не SOL -- полоса только одношаговая (3).

**Пары «Bloom против нашей».** Пар 1, из них мы раньше 0; медиана разницы -11.4 мс (плюс -- мы раньше).

**Тень.** Записей 86, по вердиктам: {"would_pass": 23, "нет вердикта": 63}. Медиана сборки 0.84 мс, симуляции 15.9 мс.

**Узкий фильтр по налогу маршрута.** Пропусков 1, тень измерила 0: цена через 28.8 с была ниже входа в 0 случаях (фильтр сберёг), выше -- в 0 (фильтр отнял), неизвестна в 1. Оценка сбережённого при размере 0.2 SOL: —.

**Разложение закрытых сделок с 2026-09-24T00:00:00Z.** Сделок 28, сумма итога -0.025437211 SOL, медиана -0.0 SOL. По каждой сделке в JSON: налог в SOL (и откуда взята ставка), комиссия Bloom, чаевые обработчику, приоритет, комиссия сети и остаток. Комиссия пула НЕ посчитана: ставки bps по программе пула нет ни в кэше, ни в репозитории, поэтому остаток -- это ход цены минус комиссия пула.

## Ответы на вопросы владельца (числа)

Данные: кэш `crowd_metric_2026-09-24.json` — 346 покупок 18 источников за 7 дней
(окно 2026-09-17T16:02Z … 2026-09-24T16:02Z), `c2_block_position_2026-09-24.json`
(150 сделок двух лидеров, 339 строк первых покупателей), `solana_transfer_fee_audit.json`,
`solana_tax_groups.json`, `solana_tax_robustness.json`, наши журналы службы.
Машинные результаты: `data/night_edge.json`, `data/night_toxic.json`,
`data/night_leader.json`, `data/night_p1.json`, `data/night_p2.json`.

### 1. Есть ли тут деньги на самом деле — ДА на тех данных, что есть, с оговоркой о покрытии

Модель нашей сделки 0.2 SOL, выход по таймеру (ближайшая реальная точка кэша — 30 с),
издержки: Bloom 1 % на сторону, чаевые 0.002 SOL на сторону, сеть, налог по ставке минта.
Полный результат посчитан для **132 сигналов из 346 (38 %)**:

| | среднее | медиана | доля в плюс | сумма |
|---|---|---|---|---|
| все 132 | **+0.0675 SOL** | +0.0255 | 72.7 % | +8.91 |
| без 3 лучших (129) | +0.0522 | +0.0230 | 72.1 % | +6.73 |
| без 5 лучших (127) | +0.0476 | +0.0223 | 71.7 % | +6.04 |
| первая половина окна (62) | +0.0860 | +0.0467 | 82.3 % | +5.33 |
| вторая половина окна (70) | +0.0511 | +0.0158 | 64.3 % | +3.57 |

Бутстрэп 95 % для среднего (2000 повторов, сид 20260925): **[0.046; 0.091]** — ноль снаружи,
в обеих половинах тоже. Вывод: **край есть и держится не на паре сделок**.

Чего в этом выводе нет: входы (i) сразу за источником, (ii) голова S+1, (iii) S+2 дали
ОДНИ И ТЕ ЖЕ числа — в кэше одна цена после сделки источника (`spot_after`), цены на
границе слотов нет, интерполировать запрещено. Комиссия пула не вычтена вовсе (ставки bps
по программе пула нет ни в кэше, ни в репозитории) — реальный результат ниже на эту
величину. 214 сигналов из 346 выпали (неизвестен налог минта или нет цены после сделки),
и эта нехватка не случайна: новые минты чаще отсутствуют в справочнике налогов.

### 2. Какие токены токсичные — групп хуже −5 % НЕТ даже после прогона по цепи

Проверено 11 признаков (котировка, тип пула, дробление маршрута, упавшие транзакции в
блоке, налоговость, доля глубины, размер входа источника, толпа). **Ни одна группа при
n ≥ 10 не дала среднего ниже −5 %** на горизонте 30 с. Настоящий токсичный сигнал в этих
данных один: **18 покупок из 346 (5.2 %) вообще без цены через 30 с, потому что
ликвидность пула сняли до этой точки**.

Что сравнимо (среднее роста за 30 с, n по группе): налоговые минты +61.7 % (91) против
безналоговых +36.5 % (82); котировка SOL +29.2 % (83) против «другой» +80.4 % (222);
Raydium CPMM +79.7 % (170) против Pump AMM +34.8 % (68); толпа выше медианы +95.3 % (125)
против ниже +48.7 % (131). То есть **30 секунд — слишком короткий горизонт, чтобы
токсичность проявилась в цене**: в этом окне доминируют сама покупка источника и толпа.

Подъём налога найден у 3 минтов (50 → 100 bps, все PreStocks одного эмитента); в наших
закрытых сделках они были промежуточными и удержали 0.0246 SOL.

**Прогон по цепи (4186 кредитов, все шесть этапов пройдены целиком)** добрал то, чего в
кэше не было: право менять эмиссию, право замораживать, право менять налог, долю топ-10,
возраст токена и пула, толпу ровно в слоте источника и в S+1, объём каждого копировщика.
Групп со средним ниже −5 % при n ≥ 10 **по-прежнему ноль**. Что видно в числах
(среднее роста за 30 с):

| признак | группа | n | среднее | медиана |
|---|---|---|---|---|
| право менять эмиссию | отозвано | 343 | +65.6 % | +39.4 % |
| | активно | 3 | +15.6 % | +15.6 % |
| право менять налог | активно | 199 | +84.4 % | +56.3 % |
| | отозвано | 10 | +64.8 % | +46.9 % |
| налоговость (по цепи) | налоговый | 209 | +83.4 % | +54.8 % |
| | без налога | 137 | +35.3 % | +18.1 % |
| возраст пула | старше медианы | 31 | +161.9 % | +83.7 % |
| | младше медианы | 30 | +23.1 % | +18.8 % |
| толпа в слоте S и S+1 | выше медианы | 184 | +74.0 % | +52.6 % |
| | ниже медианы | 162 | +55.9 % | +21.6 % |
| доля топ-10 | выше медианы | 159 | +61.8 % | +42.8 % |
| | ниже медианы | 187 | +68.1 % | +35.7 % |

Право замораживать отозвано у всех 346 — как признак оно ничего не делит. Обратите
внимание на направление: налоговые и «молодые пулы» выглядят ЛУЧШЕ, а не хуже, на этом
горизонте. Это ещё один довод, что 30 секунд меряют импульс покупки и толпу, а не
качество токена.

### 3. Есть ли манипуляции — ДА по снайперам, платят НЕ пропорционально объёму

Первых покупателей после источника: 339 записей, 120 уникальных кошельков; топ встречается
23 раза (за двумя источниками сразу). Названный владельцем `7JVQMwRj…` найден 4 раза, все за
лидером; `GUiSJYdAs…` в этом кэше ни разу — данных нет, а не «не участвует».

Сверка по цепи трёх блоков сделки PICKAXE (3 кредита, `data/night_p2.json`):

| кто | слот | место в блоке | µlamports/CU | приоритет | между лидером и им |
|---|---|---|---|---|---|
| лидер | 450191590 | 935/1083 | 273 608 | 0.000117 SOL | — |
| снайпер 1 | 450191590 | 1032/1083 | 96 764 | 0.00001 SOL | 96 tx |
| снайпер 2 | 450191590 | 1062/1083 | 41 095 890 | 0.03 SOL | 126 tx |
| мы (Bloom) | 450191591 | 688/1150 | 2 299 769 | 0.000995 SOL | 835 tx |
| DBot | 450191592 | 1359/1687 | 1 568 810 | 0.00051 SOL | 2656 tx |

В этих же трёх блоках `GUiSJYdAs…` заплатил **1.0 SOL**, `7JVQMwRj…` — **0.2408 SOL**
чаевыми на tip-счета **Helius Sender Max** (`2nyhqdwK…`, `4ACfpUFo…` — подтверждено и по
коду, и по снятой странице документации). То есть снайперы сидят на том же Sender, что и
наша полоса, но платят в 240–1000 раз больше. **Ответ на вопрос «фиксированно или пропорционально» получен по цепи** (339
getTransaction, объём каждого копировщика посчитан его же балансами): плата **растёт
вместе с объёмом** — корреляция r = 0.55 на 321 паре, наклон ≈ 164 млн лампортов платы на
1 SOL объёма, то есть около **0.16 SOL платы на каждый 1 SOL сделки**. Свободный член
отрицательный (−0.42 SOL): на маленьких объёмах плата почти нулевая и резко растёт с
размером. Коэффициент вариации платы 4.37 — то есть это не фиксированный тариф.
Практический вывод для нас: чтобы попасть в слот лидера, конкуренты платят порядка
**шестой части объёма сделки**; при нашем размере 0.2 SOL это 0.03 SOL — и тогда сама
сделка перестаёт иметь смысл.

Отдельно по цепи: наш перевод 0.00297688 SOL в покупке PICKAXE ушёл на
`B1dozJAUae1MfexMMoCzrLtTd4JKfJTHLrRfUQswQG5m` — это ровно 1 % от фактической траты
плюс processor_tip 0.001, то есть **сбор Bloom, и он берётся с каждой сделки**.

### 4. Какое правило пропуска оставить — одно: маршрут ≥ 3 шагов

Из 17 проверенных правил (по одному и парами, подбор на первой половине окна, проверка на
второй) переживает проверку одно: **пропускать, если маршрут источника дробился
(прокси ≥ 3 шагов)**. На второй половине отсечённые дают среднее −0.0077 SOL (доля в плюс
38.5 %, сберегли ≈ 0.10 SOL), оставленные +0.0644 [0.035; 0.097]. На всём окне отсечённый
сегмент статистически нулевой ([−0.013; +0.017]) — то есть пропуск не стоит ожидаемой
прибыли и убирает объективно более рискованный маршрут.

Не рекомендованы числами: «налог > 0» (отсечённые в плюсе и на train, и на test — упустили
бы прибыль), «покупка < 10 SOL» (направление переворачивается между половинами),
«котировка ≠ SOL» (отсекает 74 % объёма, и он в плюсе). Правило «толпа выше порога» было бы
**вредным**: там лучшие сделки. «Возраст токена» не оценён вообще — в данных нет времени
создания минта.

Наш **узкий фильтр по налогу маршрута** (пункт 4 ночного задания) устроен строже
статистики выше и это сознательно: он режет не «налог вообще», а маршруты, где налог
берётся дважды — случай PICKAXE. За ночь его пропусков: см. боевую часть выше.

### Строка по StonkFun

**Проверено по цепи целиком, 11 кредитов.** Из 149 налоговых минтов наших сигналов
**97.99 %** имеют один и тот же кошелёк в `transferFeeConfigAuthority` и
`withdrawWithheldAuthority`: `5KXDF6QnqhBj72hDtJNkkpFaQVUfbFXNybMsp3DiK6tD`. Все шесть
названных владельцем монет — **SANTA, CRACKER, PURPLE, RED, GP и PICKAXE** — принадлежат
этому же кошельку. То есть почти вся налоговая часть наших сигналов — это одна площадка,
и её сборщик по внешним источникам продаёт собранный налог в тот же пул.

Наши сделки через GP — худшая измеренная группа: **2 из 11 в плюс, −1.18 SOL, медианный
налог маршрута 15.5 %**. Вывод: маршрут через GP копировать только при ожидаемом росте
явно выше ~15 %, иначе он проигрышен структурно, до всякого проскальзывания.

### Лидер Beqv6 (задача E)

Обычные монеты у него крупнее налоговых: медиана входа **37.65 против 17.77 SOL** (+112 %) —
гипотеза владельца о размере **подтверждена**. Вторая её половина (что именно там плюс)
**не подтверждена и не опровергнута**. Прогон по цепи начат (20 000 кредитов) и
остановлен по пределу: он успел восстановить всего 6 «закрытых сделок», и они выглядят
негодными для вывода — удержание в них 685 тыс. … 1.69 млн слотов, то есть 3–8 суток,
чего в семидневном окне быть не может. Значит стыковка покупок с продажами в этой версии
кода ошибочна (скорее всего первая покупка склеивается с поздней продажей), и числа
−27.2 / −14.9 SOL по группам я НЕ считаю результатом лидера. Вопрос «где его плюс»
остаётся открытым. **Стыковку я починил в коде, но прогон не делал** — кредиты только по вашему
слову. Что изменилось: подписи теперь спрашиваются не у кошелька лидера целиком, а у его
**токен-счёта по этому минту** (адрес берётся из самой покупки — после полной продажи счёт
закрывают, и через `getTokenAccountsByOwner` его уже не найти), с `until` = подпись покупки,
и дальше от старых к новым до первой продажи. Порядок гарантирует, что найденная продажа —
именно первая, а не поздняя. Цена вопроса: около **760 кредитов на все 127 сделок** против
20 000+ у прежнего обхода кошелька. Самопроверка модуля 59/59. PICKAXE у него — не первый вход: в окне уже
было 3 подтверждённых первых входа в этот минт, значит транзакция 25.09 — минимум четвёртый цикл.

### Кредиты Helius за исследование (ваш предел 200 000, стоп на 140 000)

P2 — 3, выходы лидера (частично, остановлено пределом) — 20 000, G1 целиком — 11,
прогон токсичности по цепи — 4 186, прочее — 300. **Итого около 24 500 кредитов**, то есть
12 % квоты исследования. Боевым службам квота не урезалась: детектор за сутки
израсходовал 1 365 из 150 000, сторож 24 из 15 000.

### Сухой прогон полосы на настоящей сделке (проверка перед первой живой отправкой)

Полоса прогнана на транзакции источника по PICKAXE без отправки: модули на хосте
загрузились, пул опознан как Raydium CPMM, и полоса честно отказала с причиной
«котировка пула не SOL -- полоса только одношаговая». Это правильный отказ: у PICKAXE
котировка GP, а не SOL. Путь сборки на хосте работает; полоса ждёт сигнала с пулом,
котированным в SOL.

### Почему полоса за ночь ничего не отправила — котировка пула не SOL

Это не поломка и не осторожность: полоса **одношаговая** (SOL → токен в одном
пуле), а наши покупки почти всегда идут через пул, котированный **не в SOL**.

Числами за ночь:

| что | число |
|---|---|
| покупок Bloom с 24.09 | 8 |
| из них полоса позвана (с момента, как она встала) | 2 |
| дошло до подписи и отправки | 0 |
| причина в обоих случаях | «котировка пула не SOL -- полоса только одношаговая» |
| пулы этих двух покупок | Pump AMM `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`, Raydium CPMM `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` |
| тень по всему потоку сигналов: «нужен второй шаг» | 47 записей из 57 несобранных |
| в кэше 7 дней: котировка «другая», не SOL | 222 покупки из 346 |

То есть предел один и тот же и в боевом, и в замере, и в истории: **одношаговой
покупкой мы достаём не больше четверти сигналов**, а сегодняшние два выпавших
на полосу оказались как раз из большей части. Ждать «своего» сигнала можно, но
гарантии на ночь нет.

Решение за владельцем, и оно про деньги:

1. **Оставить как есть** — полоса вооружена (`BLOOM_OWN_SEND_LIVE=1`, 0.01 SOL,
   симуляция перед каждой отправкой) и ждёт покупку с котировкой SOL.
2. **Разрешить двухшаговый маршрут полосы** (SOL → котировка → токен). Это
   правка денежного кода, и без вашего слова я её не делал.

Чтобы второй пункт решался числами, а не на слух, с 03:37Z включён **кэш ног
двухшаговой тени** (4 котировочных пула, 32 таблицы адресов прогреты): к утру
будет видно, сколько сигналов двухшаговая сборка **собрала бы** и сколько из них
прошло бы симуляцию. До этой ночи кэш был выключен (`BLOOM_SHADOW_LEGS=0`),
поэтому все 47 записей «нужен второй шаг» остались без замера.

### Две ошибки в своём коде, найденные и починенные ночью

1. **Тень пропуска ничего не мерила.** На первом же живом пропуске
   `SKIP_TAXED_ROUTE` вызов пула шёл с именем параметра `кошелёк` вместо
   `кошелёк_источника`, и вместо цены в журнале лежал `TypeError`. Заглушка в
   самопроверке брала любые аргументы и отдавала строку вместо словаря —
   поэтому ошибка и прошла мимо тестов. Заглушка теперь повторяет настоящую
   подпись один в один; добавлен случай «пропуск без названного пула». И
   «измеренным» пропуск теперь считается только если в записи есть точки цены:
   неудачная попытка больше не закрывает пропуск навсегда (предел — три
   попытки, каждая около пяти кредитов).
2. **Доклад прятал работу полосы.** Пути считались по `stage == "own_send"`, а
   это поле в записи затирается стадией самой отправки (`build`, `sim`,
   `send`): две настоящие попытки выглядели как «путей 0». Отбор теперь по
   метке `lane`, и в текст добавлены стадия и причина остановки. Плюс в признаке
   жизни появились отдельные числа `starts` (сколько раз полосу позвали) и
   `threads_alive`: без них «покупка была, а путей ноль» нельзя было отличить от
   «полосу не позвали».

### Место в блоке у наших покупок — считается, и вот первое живое число

Модуль места в блоке до 02:15Z на хост не доставлялся, поэтому у всех покупок
с 22:17 по 01:15 в позиции стоит `block_why_not: ModuleNotFoundError` (и после
трёх попыток позиция сдалась). Это старые записи, а не текущее состояние: после
доставки первая же покупка замерилась.

Покупка 02:45:33Z (минт `C2WtD354…`, источник Beqv6, маршрут Raydium AMM v4 →
Orca Whirlpool → Raydium CPMM): наш блок **S+1** относительно источника, место
**93 из 1391** (доля 0.067), круг Bloom 33.8 мс, наша транзакция в потоке через
598.85 мс от решения. Для сравнения, в разобранной по цепи сделке PICKAXE мы
стояли 688 из 1150 в S+1 — то есть в этот раз мы попали в начало блока, а не в
середину.

Заодно исправлено, почему такую поломку нельзя было починить по журналу: причина
отказа писалась одним словом `ModuleNotFoundError` без имени модуля. Теперь в
причине стоит текст исключения, а в признаке жизни есть раздел `block_position`
(«модуль загружен / не загружен и почему») — он виден в отчёте деплоя ещё до
первой покупки. На текущем хосте `module_loaded: true`.

### Двухшаговая тень: вооружена, но к 04:52Z мерить пока нечего

Кэш ног включён с 03:37Z и жив: 4 котировочных пула, шаблоны свежие (возраст
0.3–86 с), 32 таблицы адресов прогреты, 13 340 скормленных уведомлений, 0 сбоев.
Но замерять двухшаговый маршрут не на чем: **после 04:11Z ни одного сигнала вида
«покупка» не пришло**. Видно по журналу — за 40 минут выросли только `NOT_A_BUY`
(172 → 185), а все три кода, которые ставятся на покупках источника
(`TARGET_AMOUNT_OUT_OF_RANGE` 105, `SKIP_TARGET_INCREASE_POSITION` 104,
`TOKEN_RECEIVED_NOT_BOUGHT` 88), не изменились ни на единицу. Часовое окно тени:
0 сигналов, 0 собранных — тревоги нет и не должно быть, её порог начинается с
трёх сигналов.

То есть числа «сколько бы собрал двухшаговый маршрут» появятся, как только
источники снова начнут покупать. Всё, что для этого нужно, уже стоит на хосте.

Цена замера, честно: подписка на 4 хранилища котировок стоит около **11 000
кредитов в час** и тратится, даже когда сигналов нет (это подписка, а не запрос
на сигнал). Свой предел у замера 30 000, при нём он выключается сам и называет
причину; на 04:11Z израсходовано 6 034, то есть выключится он примерно к 05:45Z.
Суточный расход детектора на 04:11Z — 22 697 из 150 000.

### 05:11:01Z — полоса отправила свою первую живую покупку, и на ней же нашлась денежная странность

Сигнал: источник `DAejzMs5cUeCCENNvapy9KWFwzwegh7LvcgNkZ6hnf1y` (BATCH-3) купил
на **2500 USDC ≈ 21.45 SOL** минт `571aBZbC…pump` на Pump AMM, слот 450254305,
маршрут один шаг, налог маршрута 0 bps — то есть первый за ночь сигнал, который
прошёл и узкий фильтр, и требование «котировка SOL».

**Пара «Bloom против нашей»** (оба в одном слоте 450254306, то есть S+1 к источнику):

| | Bloom | наша полоса |
|---|---|---|
| круг до отправки | 24.6 мс | сборка 0.745 мс + симуляция 52.95 мс + подпись 17.99 мс + отправка 5.79 мс = **89.39 мс** |
| от решения до появления в потоке | 759.39 мс | 700.32 мс |
| слот | 450254306 (S+1) | 450254306 (S+1) |
| место в блоке | **26 из 1059** (доля 0.025) | ещё не добрано |
| размер | 0.2 SOL | 0.01 SOL |
| разница пары | — | **−11.4 мс** (минус = мы позже Bloom) |

Симуляция прошла (`would_pass`, 113 367 CU), чаевые ушли на счёт Sender
`5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn`, подпись нашей транзакции
`5dkvzvRve78v89ZToTxdxG6bFVjdy7wnJ3vTBKd3YC96C7dWDsA2QPtjJqJMkZYo53XQvDggf1D22eKY5NNtv16B`,
blockhash был свежий (2.92 с). Ожидалось 3 727 700 661 raw, пришло
**3 453 513 816 raw** — на 7.4 % меньше котировки, при пределе проскальзывания 35 %.

**И тут же — расхождение учёта остатка.** Через 28.8 с автоордер Bloom продал
**весь балансе кошелька по этому минту**, включая токены полосы: когда до позиции
полосы дошёл наш сторож, остаток минта был 0, и позиция закрылась с записью
«продавать нечего: купленное полосой 3453513816, остаток минта 0». Выручка
(0.080829816 SOL на вход 0.2 SOL) целиком легла в учёт позиции **Bloom**, то есть
итог полосы посчитать нечем: `closed_sol_net` пуст, PnL полосы неизвестен.

По вашему ночному правилу («расхождение учёта остатка — KILL полосы, строка в
Telegram, без владельца не чинить») я:

1. **поставил рубильник полосы** `KILL_OWN_SEND` с этой причиной — служба его
   подтвердила в признаке жизни (`lane_kill: [true, …]`);
2. отправил строку в основной чат Telegram;
3. **ничего в логике не менял** — решение за вами.

Bloom, сторож продаж и тень работают как раньше. Для рубильника снаружи сделан
отдельный прогон `run_vps_lane_kill_nl.yml` (только `status` и `set`; снятия
запрета в нём намеренно нет).

Что это значит по существу: **полоса не может держать позицию в том же минте, что
покупает Bloom** — автопродажа Bloom через 28.8 с сметает весь баланс кошелька.
Варианты на утро (оба про деньги, поэтому без вас не трогаю):

1. **Отдельный кошелёк для полосы.** Тогда её вход, остаток и выход считаются
   честно и ни с чем не смешиваются. Нужен перевод SOL на новый адрес и ключ в
   секрете — это ваше действие.
2. **Полоса только на минтах, которых Bloom не покупает.** Дешевле, но тогда
   пропадает сам смысл замера «Bloom против нашей на одном сигнале».
3. Считать итог полосы расчётно из доли (наши 3.45e9 из общего проданного
   количества). Это оценка, а не факт цепи; я бы так деньги не мерил.

## P1 (тень против Bloom)
```json
{
  "ok": true,
  "built_utc": "2026-09-25T05:24:40Z",
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
    "excluded_not_bloom_lane_or_dry": 1,
    "excluded_before_since": 1,
    "total_bloom_trades": 33,
    "collected": 3,
    "collected_share": 0.0909,
    "with_diff_pct": 1,
    "median_diff_pct": 6.028
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
      "sh
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
  "generated_utc": "2026-09-25T01:56:55Z",
  "elapsed_s": 0.86,
  "input_files": {
    "crowd": "/tmp/claude-0/-home-user-robinhood-chain-alpha/1766891f-706e-5008-a349-9de1fb730381/scratchpad/c2/crowd_metric_2026-09-24.json",
    "tax_groups": "data/solana_tax_groups.json",
    "transfer_fee_audit": "data/solana_transfer_fee_audit.json",
    "leg_pools": "/tmp/claude-0/-home-user-robinhood-chain-alpha/1766891f-706e-5008-a349-9de1fb730381/scratchpad/c2/c2_leg_pools_2026-09-24.json"
  },
  "window": {
    "window_from_utc": "2026-09-17T16:02:53Z",
    "window_to_utc": "2026-09-24T16:02:53Z",
    "n_sources": 18,
    "n_trades_raw": 346,
    "generated_utc": "2026-09-24T16:24:26Z"
  },
  "n_signals_total": 346,
  "n_with_known_net_t28_8": 132,
  "n_with_known_net_t60s": 133,
  "HONEST_CAVEATS": [
    "Входы (i)/(ii)/(iii) численно совпадают: единственная реальная цена после источника в кэше -- spot_after; цены на границе слотов S+1/S+2 нет, интерполяция запрещена условием задачи. См. докстринг модуля.",
    "Таймер 28.8с считается по ближайшей реальной точке 30с (growth_after_30s), без интерполяции; разница ~1.2с (~4% таймера) объявлена, не скрыта.",
    "Горизонты 10с/5мин/до первой продажи источника отсутствуют в кэше целиком и везде помечены missing/why_not -- не 0 и не оценка.",
    "Комиссия пула (swap fee AMM) НЕ включена в net_sol: в кэше и репозитории нет ставки bps по pool_program, только ярлыки программ. Реальный чистый результат ниже посчитанного на эту неизвестную величину.",
    "Налоговый статус минта 'неизвестно' (минта нет в solana_transfer_fee_audit.json) НЕ считается 'без налога' -- такие сделки помечены missing и не входят в среднее/сумму/долю в плюс (214 из 346 сделок без полного net на горизонте 28.8с).",
    "'Возраст токена' и 'до первой продажи источника' не оцениваются вообще: ни в одном переданном файле нет времени создания минта или времени продажи источника.",
    "'Число шагов маршрута' и 'глубина пула' в правилах D -- ПРОКСИ по имеющимся полям (split_route, глубина пула КОТИРОВОЧНОГО минта к SOL, а не пула самого таргет-минта), не измеренные величины; см. hops_proxy/leg_pool_sol_depth в докстрингах."
  ],
  "A1_signals": [
    {
      "signature": "hkQJPAjvSTeKwb59iJCEgMkjH66LtsBrMZJHoKjNjiZJVogKtb4N71Emi4x2B5ZaFdAy5jGgaHaSPhsUrKu8gFB",
      "source": "Theo",
      "source_address": "EC2f5DnHzuNRit1ExqghSifDbp1wgrzktsRRZCtU92MJ",
      "block_time": 1789712053,
      "block_time_utc": "2026-09-18T06:14:13Z",
      "mint": "Atf7KaBLF257KQosBM2rCxhhdRfDtRNnpwmUH4bkrMMV",
      "entry_price_spot_after": 0.024186518339649906,
      "no_spot_after": false,
      "t28_8": {
        "net_sol": null,
        "missing": true,
        "why_not": "налоговый статус минта неизвестен (нет в transfer_fee_audit)"
      },
      "t10s": {
        "net_sol": null,
        "missing": true,
        "why_not": "в кэше нет цены на 10 с (только точки 0/30/60 с) -- не интерполируется"
      },
      "t60s": {
        "net_sol": null,
        "missing": true,
        "why_not": "налоговый статус минта неизвестен (нет в transfer_fee_audit)"
      },
      "t5m": {
        "net_sol": null,
        "missing": true,
        "why_not": "в кэше нет цены на 5 мин (только точки 0/30/60 с) -- не интерполируется"
      },
      "until_source_first_sell": {
        "net_sol": null,
        "missing": true,
        "why_not": "в кэше нет времени/цены первой продажи источника (только его покупки)"
      }
    },
    {
      "signature": "3NnPjwvKmXHBogzAK5weD4G6MrXEQHJsooncJTfkAK1LBT9GCgDq24Utqx6qeBMfNcyykvfrdK6N2pFt5Lf1UixG",
      "source": "Theo",
      "source_address": "EC2f5DnHzuNRit1ExqghSifDbp1wgrzktsRRZCtU92MJ",
      "block_time": 1789717180,
      "block_time_utc": "2026-09-18T07:39:40Z",
      "mint": "AmBKmofV3uvYfaaFAYqwZZXhdQfU2neCHW2Tk3eaNAFk",
      "entry_price_spot_after": null,
      "no_spot_after": true,
      "t28_8": {
        "net_sol": null,
        "missing": true,
        "why_not": "
```

## B/C (токсичность, манипуляции)
```json
{
  "schema_version": 1,
  "generated_utc": "2026-09-25T03:10:51Z",
  "inputs": {
    "crowd_file": "/tmp/night_state/crowd_metric.json",
    "followers_file": "/tmp/night_state/followers.json",
    "tax_catalog_file": "/home/ghrunner/actions-runner/_work/robinhood-chain-alpha/robinhood-chain-alpha/data/solana_transfer_fee_audit.json",
    "tax_groups_file": "/home/ghrunner/actions-runner/_work/robinhood-chain-alpha/robinhood-chain-alpha/data/solana_tax_groups.json",
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
        "getTransaction_refetch_route_and_exact_pool_depth": 346,
        "getAccountInfo_mint_authority_tax_max_fee": 273,
        "getTokenLarg
```

## E/F/G (лидер, GP, StonkFun)
```json
{
  "schema_version": 1,
  "generated_utc": "2026-09-25T02:57:37Z",
  "inputs": {
    "crowd_cache": "/tmp/night_state/crowd_metric.json",
    "state_dir": "/tmp/night_state",
    "audit": "data/solana_transfer_fee_audit.json",
    "tax_groups": "data/solana_tax_groups.json",
    "bloom_report": "data/bloom_report.json",
    "santa_autopsy": "data/solana_santa_trade_autopsy.json",
    "mint_extensions_cache": "data/solana_buyer_200/prior/current/solana_three_check/metadata_accounts.json"
  },
  "constraints": {
    "read_only": true,
    "no_trades": true,
    "chain_reads_via": "rpc_call, передаваемый вызывающим (см. c2_shadow_build.py); --chain строит его через c2_common.C2Rpc с потолком кредитов"
  },
  "chain": {
    "ok": true,
    "service": "c2_night_leader_stonkfun",
    "key_env": "HELIUS_API_KEY",
    "local_credit_limit": 500,
    "owner_stop_fraction": 0.7,
    "owner_stop_credits": 140000,
    "c2_daily_budget": 200000,
    "credits_used": 11,
    "calls_used": 11,
    "stopped_reason": null
  },
  "task_e_leader": {
    "ok": true,
    "leader": "Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit",
    "leader_meta": {
      "task": "BATCH-5",
      "remark": "pointfarmcap",
      "n_trades": 127,
      "window_from_utc": "2026-09-17T16:02:53Z",
      "window_to_utc": "2026-09-24T16:02:53Z",
      "window_days": 7.0,
      "n_signatures_window": 6662,
      "crowd_cache_generated_utc": "2026-09-24T16:24:26Z"
    },
    "groups_n": {
      "tax": 47,
      "normal": 10,
      "unknown": 70
    },
    "groups": {
      "tax": {
        "entry_size": {
          "n": 47,
          "n_with_size": 47,
          "median_sol": 17.770794,
          "mean_sol": 25.820391
        },
        "price_proxy_30_60s": {
          "median_growth_30s": 1.640115,
          "median_growth_60s": 1.571861,
          "median_growth_after_30s": 1.439146,
          "n_growth_30s": 45,
          "growth_after_30s_is_not_pnl": true
        }
      },
      "normal": {
        "entry_size": {
          "n": 10,
          "n_with_size": 10,
          "median_sol": 37.645745,
          "mean_sol": 39.018757
        },
        "price_proxy_30_60s": {
          "median_growth_30s": 1.444717,
          "median_growth_60s": 1.334965,
          "median_growth_after_30s": 1.240814,
          "n_growth_30s": 10,
          "growth_after_30s_is_not_pnl": true
        }
      },
      "unknown": {
        "entry_size": {
          "n": 70,
          "n_with_size": 70,
          "median_sol": 7.445012,
          "mean_sol": 8.993396
        },
        "price_proxy_30_60s": {
          "median_growth_30s": 1.898878,
          "median_growth_60s": 2.017641,
          "median_growth_after_30s": 1.819884,
          "n_growth_30s": 67,
          "growth_after_30s_is_not_pnl": true
        }
      }
    },
    "hypothesis_size_bigger_on_normal": {
      "ok": true,
      "median_size_tax_sol": 17.770794,
      "median_size_normal_sol": 37.645745,
      "normal_bigger": true,
      "diff_pct_of_tax": 111.840535,
      "profit_part_confirmed": false,
      "profit_part_why_not": "нужен итог ЗАКРЫТОЙ сделки лидера (вход/выход в SOL-экв после налога) -- выходов в кэше толпы нет, см. exit_reconstruction"
    },
    "pickaxe": {
      "ok": true,
      "n_prior_confirmed_entries": 3,
      "prior_entries": [
        {
          "signature": "3yEeBVUMg4QJhQJHuiprLuHKUHzGg7EpNtNMeMkK4qQ5ZqoftiAbRaHvwFpAxM3xLfyKSe3iexCtk4MWjT4bvZk2",
          "block_time_utc": "2026-09-22T01:26:34Z",
          "spend_sol_equiv": 4.252569
        },
        {
          "signature": "JVjCgJQ1sR2GAA8h6DWwe7HMPskoxAR7roMoYWNELLxiHChsQKMrakHGVkKThad6c152E4mBYFQGXyxeNATRaan",
          "block_time_utc": "2026-09-23T00:55:49Z",
          "spend_sol_equiv": 8.412632
        },
        {
          "signature": "3VCvVA97A7jKzSW47W51wXxZ8XxgrH1Z9keiEEDb8aSfuYJkTTLkke545qrxakCTBM6ryzenGoj2EbGWg3yGpET8",
          "block_time_utc": "2026-09-23T20:21:40Z",
          "spend_sol_equiv": 6.671219
        }
```

## Что из данных НЕ следует

Прочерк в таблице -- это не ноль: там, где стоит «—», величина не измерена, и подставлять вместо неё ноль нельзя. Оценка сбережённого фильтром считается по цене пула через 28.8 с и при размере сделки 0.2 SOL: это модель одного горизонта, а не факт нашей сделки, которой не было. Разница пары «Bloom против нашей» измерена по времени появления транзакций в подписке processed нашего узла; это наш узел, а не общее время сети.
