"""SC1, дозапрос владельца (2026-09-01): срез через публичный RPC
(`https://rpc.mainnet.chain.robinhood.com`, без ключа, см.
`analysis/alchemy_fallback.py::_endpoints()`) -- применяет
пре-регистрированный критерий кошельков-повторников (`docs/SC1_NOTE.md`,
«Пре-регистрация: критерий концентрации кошельков-повторников...»,
2026-09-01T21:28:48Z, ДО какого-либо измерения):

    >50% -> KILL; <20% -> главная линия; между 20% и 50% -> третий
    кластер (обновлено владельцем в этом дозапросе -- теперь ДВА
    кластера уже задействованы, main + control, поэтому "между"
    требует ТРЕТИЙ, не второй).

СТАТУС (2026-09-01): запускается по прямой команде владельца ("запустить
сейчас"). Ключ не требуется -- публичный RPC работает без него
(rate-limited, ретрай/фолбэк встроены в alchemy_fallback.py).

============================================================
Методология выборки (владелец, этот дозапрос)
============================================================

**MAIN**: случайные 50 токенов кластера `0x0eaced04ec017ea0d9985b6bcd16657b5b2dac78`
(1641 токен всего) -- та же цель, что и предыдущий дозапрос (топ-10 по
$/день лидер), но теперь СЛУЧАЙНАЯ выборка вместо топ-20 по объёму
(убирает смещение отбора по объёму).

**CONTROL**: случайные 50 токенов кластера
`0x376d633018680caa4ec3f3e735a2797abf7f9cb2` (481 токен всего, 2-й в
топ-10 по $/день, тоже "плоский" профиль по внутрикластерному разбору,
см. `docs/SC1_NOTE.md`) -- для сравнения, та же методология,
одинаковый размер выборки.

**Seed = 42** -- ПЕРЕИСПОЛЬЗУЕТСЯ `CONFIG.random_seed` (`analysis/
config.py`, используется по всему проекту с самого начала, Sprint 1) --
осознанно НЕ новый/подобранный seed, чтобы не создавать даже видимость
подгонки выборки под желаемый результат. Сэмплирование
(`random.Random(42).sample(...)`) сделано ДО запуска, зафиксировано в
коде (списки `MAIN_SAMPLE`/`CONTROL_SAMPLE` ниже) -- воспроизводимо,
проверяемо, коммитится ДО реального прогона (тот же принцип
пре-регистрации, что и весь остальной SC1).

Честная оговорка: «кошелёк с ≥5 токенами» проверяется ТОЛЬКО в
пределах наблюдаемой выборки (50 токенов), не всего кластера (1641/481)
-- реальное число различных токенов кластера у кошелька может быть
ВЫШЕ (мы не видим остальные ~1591/431 токена) -- то есть это НИЖНЯЯ
ГРАНИЦА признака "повторник", не полная картина. Это делает метрику
консервативной в сторону "главной линии" (реже находит повторников,
чем было бы видно по всему кластеру), не в сторону KILL.

============================================================
Дополнительные метрики (владелец, "из тех же логов, без новых запросов")
============================================================

- Общее число уникальных торговавших адресов (по выборке).
- Доля объёма у топ-3 адресов (по объёму, НЕ обязательно те же, что
  "повторники" -- отдельная сортировка).
- Медиана числа ВНЕШНИХ покупателей на токен -- "внешний" = НЕ входит
  в множество адресов-деплоеров этого кластера (`sc1_deployer_to_cluster.csv`,
  тот же принцип, что исходный паспорт SC1, Шаг 3: "доля токенов
  кластера с нулём внешних покупателей (покупатели != сам кластер)").

Наружу -- ТОЛЬКО агрегаты (владелец: "наружу только агрегаты") --
построчные логи/адреса кошельков остаются в локальном JSON-кэше,
НЕ публикуются в docs/.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from alchemy_fallback import (  # noqa: E402  (см. sys.path.insert выше)
    UNISWAP_V3_SWAP_SIG,
    _chunked_get_logs,
    _rpc_call,
    topic0,
)

CACHE_DIR = Path("data/p3_guard_cache")
OUT_PATH = CACHE_DIR / "sc1_wash_slice_result.json"

PAIR_TOKEN = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"  # WETH, подтверждено symbol() в прошлом прогоне (docs/P3_GUARD.md)
ETH_USD_PRICE = 1895.565143603286  # sc1_eth_usd_price.csv, тот же источник, что весь SC1_NOTE.md
RANDOM_SEED = 42  # CONFIG.random_seed -- см. докстринг выше

KILL_THRESHOLD = 0.50
MAIN_LINE_THRESHOLD = 0.20

# Сэмплы зафиксированы (random.Random(42).sample(...), см. скрипт
# генерации в истории сессии) -- (token, pool), отсортировано для
# детерминизма. НЕ пересчитывать заново без явного решения владельца
# -- пере-семплирование задним числом было бы нарушением
# пре-регистрации.
MAIN_SAMPLE = [
    ("0x027b4451f7d433e64996814e60baeb45455a5113", "0x84d1bdd63c6bc33e7df99687dd74c8002492b77e"),
    ("0x0789130db945a40404270f8b391fcedf0b00fbf9", "0xd2589be627bf278a413bb19b1f596a47f66deb50"),
    ("0x07f0b213f5d4c3442b3ab88b3040aed849a1fdd6", "0xcfce51206ced498d8ddae36db0777d3339f58cbc"),
    ("0x08d40c3bee04510c1be48422de7774d6a3c77fae", "0x23ec54f281e9f5e0204b1d88b78eab9f1b877417"),
    ("0x09c33f475080f55bf9a7328d7e84a6a8566586d0", "0x74faf14c8b42372458ad6312f32333e005a97d29"),
    ("0x1baf95283904c31db593785eb6c52b7ebec58362", "0x7124445ccaccb6cf39dc3196a30d33b7044bfb7b"),
    ("0x1e7fe90df7e6ff0b178ddfa35e1adda82ce38f58", "0x851526ae0cd98a3989800ae9822d64bc65195e16"),
    ("0x1e94ca9f390ae77b88a721c4dfa6bd53c5221b70", "0x44f9c3e89d08007c32c4266a3113452f2017420b"),
    ("0x1f35f3a4cc1aa8933ff44011452f4c0bc9653a15", "0x73be0e5b63f89bb1e8d28698c3de22b9a25d937e"),
    ("0x203433091548989aa487cc56ea8e193409833ff1", "0x9ea145e620ee86e39c527f56e33a53c9374753fd"),
    ("0x230642ca6bc53c5a4cc7854d1fbd77abf3a92998", "0x0ca08f2e721706de9f78549a9f3b039bab6f847e"),
    ("0x2afd22126e120b7a669fe5c8405c0a891d172d22", "0x6e9fd54f429a405065f1e306c573e8865e163aa2"),
    ("0x31e114c87bcc3bde5e9bfaa39b24f6d451678dc3", "0xf42c666c3f02c42f4b796f455d7664d0819b0b09"),
    ("0x32916412648ea71dd550253ae46252ffef903ba2", "0xa440941f85c72ba91f881b7461eb21b141b43f13"),
    ("0x3f06f58ba8fede100f25a597b3330def9ed1af9c", "0xc67ce041ded53bfa255feae72b829a58186c2319"),
    ("0x4413929bc56d53053290de7270c1bec67e5c7409", "0x21c9fe485ee57283bfc937d43645ed86b57c7e35"),
    ("0x44cfcb4a99036ca24c8715521a302cd359642c94", "0x0fb63097a84e4fb96b1f4e3cfd49ce7503a74d43"),
    ("0x455709fbdc0d41adebec7ee9e02df68124ba11dc", "0xe358387fd24526104afdd05389dbdeb439c959d6"),
    ("0x45e549be546932cc1511caecebbbd8a2c0a55845", "0xca3613d658a7f2ac362df80b06b8a666f8958216"),
    ("0x47b198eb27d3cedbf14275ca272a9862ee9749fe", "0x92580b1240d69d01c6bb9017c50c674306371830"),
    ("0x4c4fa6d62f8b24d3c939997510989e402ec6e2aa", "0xa3198f0be4167390c85220ee1d9f292dfa9b9d7a"),
    ("0x55c01d0969fd80b92acc161adb428e57dde9d58f", "0x9b5e928fa4cd851ada680d869cd9953abde1d002"),
    ("0x56d9bccf84abdd506ac2b805e64f73a272003fb6", "0x0a9bf71454298cff67abdb05740d392806edd522"),
    ("0x69179599b459ae2773dd241aaf8bbe0fe7c57a78", "0xa0d55ca51878e021f3ea0dc0bdd24719773cac2f"),
    ("0x69dc6d30f6dc5c64ef33bae0b32ed8328baa3759", "0x468399f058283f47d6b20742a44bd5b4277b0989"),
    ("0x6aafa6e6090e17068e1eeba9a6956f8c71f9f5c3", "0xc82ac63793b1619459d818208694476b9eaad5ec"),
    ("0x70877d7efbcf9ae26d654dff28df538c98e5b2bb", "0xc469580c6a0d51a1eabbe200e3b1704efaea09be"),
    ("0x755a06d5e9f9a304f60ce0685407788721a91df4", "0x3d6dbe0faa9bdc21c6825d5db03d47074e1ba78c"),
    ("0x8302fe2cc45d8cf4e8f408d457444deaa01b477f", "0x78d6b5ac16c24a6f725d6f4ea047bc3ff96608bb"),
    ("0x83a0ab228259ba0eb98aeb46c65842db31a010b8", "0x51dbb04c0a5b88a67bc952c05b7287f94e7b0b2b"),
    ("0x83bee96e74cbf3168ecadfc37967a53cef16a802", "0x8a413c846550746173101af3f4b4c437224141fa"),
    ("0x8cf68fa704ecf2b15b4c998f44294238e62d17ff", "0x729a551302d18ecf6a21f6b232a4d40b44e73174"),
    ("0xa083f8a37f5c6c22f1c61d002c671d43aa0dd92a", "0xbf36b99c1cd509b654b4f17c39c804e2e8fc628a"),
    ("0xb0892373823f2069fab090197acf0a07576cbb38", "0xd7e2a3b7dd4e04c2065e2be296d6c0bf22cb4b97"),
    ("0xb48ac0335671068c289f107059f1660e7050cd81", "0x43246b927c82d5f74b753bd47ffb9e687da252a2"),
    ("0xbd7408d148bc8ffc574208ee8d40c6928cb350da", "0x71da8d85e90ab588a91552c424bd069f5df58cd7"),
    ("0xbe0cd7df8d02694c5241d43d272990f2cf51a0c1", "0x5487678fba91c8a1772e749d34fe5c7833dbe288"),
    ("0xc2042b7c7ad91aec15454cbf1803d7cc54ea6ff9", "0x3e7802812b3c7a78bc23a50de3dacaec6034af4f"),
    ("0xc2b2186a9698fd85e23656ac09811b15b5dd9589", "0xc7de906d2b7dfa8ac4c0b48c241ff37a6f1773a3"),
    ("0xcc4a8c99afb34abd77ee8f1598178c67aafc35ee", "0x85f0d26cb8ff2880bd4cbc80333f9adfb447d8b5"),
    ("0xcf0afe6efac54e2c6473c2dac9d648c9665a6729", "0x255ee4408279ffa7107af6baa69c2aec49e58d60"),
    ("0xd69eab2437a8cb95c11e263e4ce3f0d78e9db81c", "0x6771447530182285c1f74fe4474c2f173ba5d051"),
    ("0xdd904ac15858482d071c9007388b19650f9e68eb", "0xbfca7be2b52278a441412d0c9d42e457c2dfcb69"),
    ("0xdedf14fb258f1bced72efd80128723b88a234d92", "0xc5aadd9f2328a41f55098a86cfdb4caa507fcdde"),
    ("0xe2f450df1b8d2be57fb6d43ccfe416bef7ac3fc3", "0x6bb22f739589834ae1e0c48391c27ff189a75f1d"),
    ("0xead42096dabc98ea29900fbb9a9e41a3906cc45a", "0x9856ae21d542082c84c3ae4aafb36a0aa996af7a"),
    ("0xebc10a616aadf8aea933f73c630282d70e8f44c7", "0xe5dcb78135bd2a07b01a981f2bf220565bc330fa"),
    ("0xec15ae140deea6a5b005544fdd20720d21e50dc8", "0x7a0d0dec3b173352f9edf71b84f345f167e6dc1c"),
    ("0xf127232eac184695f4f2ed5ef416e05a687b53d0", "0x95dc824573fb92c1f86268d543d4700be304e492"),
    ("0xf283ba68b3fcc6ffaa1d42d86a353b3544e57d4e", "0x98fc937f9c72f65cf09edf2d65687c40554ac9a1"),
]

CONTROL_SAMPLE = [
    ("0x00d377b5db6eaac8752aafb1ebed191f64992863", "0x98bbe014d21693dfc8f3f38d202c0d82d5a31108"),
    ("0x04b325f8cf3c5a0c63b6a170380d009c38113d76", "0xd584fbb26c6e7a7c1026cb613bb1493aa105b1b2"),
    ("0x05214fdf03c6aa8e678e3f90137b0d08eb39521d", "0x53c3b2494230fe6184657172232ea60df6828033"),
    ("0x05c93acee23706c4fbfbe20e059c7ee01b597904", "0x48174d7f5cf1d720cc4ecdd46b6c689a92e6c875"),
    ("0x05d46c19e2550ff7d634df8101f7811151fafe95", "0x3104b7124bd3ddfcc62400ce3494da56dd5903a1"),
    ("0x1726bcd7b8ab885afc08c81c3810365d7bc4d505", "0xe8392123fa09e5b5aeee3790a02caae6c30b393f"),
    ("0x183aad75ace9db09fa0a81e50b1a14d819d691db", "0x528fa746490cfd386b3b4bf6a816d2f4edf8b238"),
    ("0x191604871555ff22e6c5cdbc070a2eaf172d509c", "0x29bae0710f61da449740973b2e8d71b19a273662"),
    ("0x19ed7aa1f4703b36aea5c271a17875e7d859e8b5", "0xfc1657a70675aae49b7006dcddde1bd587a74ed3"),
    ("0x1cff548f3113b5bdb24ac3c44f840e2370bb8879", "0xc918da270c3c622667fab615be522584e13725a9"),
    ("0x230ef75e0efb375651aa939af3790be04da2767e", "0x37d54d7ff64cda9ce1d9d09c53bdf1a4b79335ff"),
    ("0x28d7811852ef467ff2da46381c04f9ecd5a474d6", "0x41d04347a7e77f0bb866230117c8cb893dde5186"),
    ("0x2a0c8d39d0ef02938fe09bc073b3e270a773118e", "0xbba8bea14c4d290b059e3c50f13aa430a4cbe6d5"),
    ("0x347980bc51029c1fc8bd6f593dd634c5f9b8dfd5", "0x18d15db1a0a4f6d000124de22e426aebb807db83"),
    ("0x389408e717d814bd25d0e6079301e538e1ba06c9", "0x60d86d417ab515afc35119563186aaef19a21995"),
    ("0x39383382c74b3249c7201576f8c4f8af24e1e251", "0x8590a4432730aa2f6d82cef7491bbf80313c1acf"),
    ("0x395fa04462d4552a28696648bdfea643f2c21ecd", "0xc2de632f2322262f3ea3e2cb599bdcc66e926b59"),
    ("0x3a96fafb738ecaf738f71dcadafd34862c5647e8", "0x4ad0aa6ab7ec917a3745e1148fcedf5c55a8afe0"),
    ("0x3eb9b9ac1c93697ac63d14f899c73d9fb0352f27", "0x6614089a8b690f0b8d51f34716fed0aabde94433"),
    ("0x433257d1cd9e84f40591db9f09d38d9cc7f43024", "0xbd87707c7384aca4070e9ba3ebbf6edb156ebef1"),
    ("0x49cc50af254f645afb3a9b8bfc24dad890c38527", "0xb09718fcc29efd31c184d573dae1ac4d3e8ddb64"),
    ("0x4b915b54f55c1672a4038b09e0e80e402f84add0", "0xc3e4fb43e06b49d21730b5d061afbcf64a76d805"),
    ("0x5c669c172b1d8336febbe28b60ba274abdf00366", "0x39068c0cde64e2c1211525692a7076acd62c56e6"),
    ("0x5d1b255e235d7e8700962b358dabab278797039e", "0x224fb74cb1927cf37b325e2bee02f4a152b75fde"),
    ("0x63b5a7b62e4eeaf1b5e7606b0125083454842a1e", "0xce8ecd762f6e55bfa0d407aabf3536f42ac03878"),
    ("0x69da4a5fca427b25b64ada92c9858ac1e4be2255", "0xd307b07f195788a7023f4e1e40e216f76112236c"),
    ("0x712f6f092b918c074b6d84dd6dd9a0c5fad06602", "0x1e5d409e37bf0ddfd9a0d3b40238c3c5d18ef8fc"),
    ("0x729a6d3d85cedccdae54caffe3b1b64ad0dc355c", "0xe0efc2727709e14b23398dd95dffeb95832841af"),
    ("0x78eb436655c7a5c46ad05135dbb9f8f203cbaa3a", "0xbed7147bec372a5d4250a9b769113a57c8cc2894"),
    ("0x8a64b0a22866215c28aa4da9e3797ec3e7d8b26e", "0xfe58bf33346cb0f4dd1ce20868f60a104d3a3bd0"),
    ("0x9b783d260a3f4ab98fed37a6c7a234eb8e02519d", "0x87e8add4fc24227a8437db167cf6660a0a8e6f1d"),
    ("0x9e37f8f27ee912f8da6355d4eb79e5069d661670", "0x058822d5087501c685ecfa090be244e59f0d568c"),
    ("0xa51bda953d430bb4c6ae135e03d51c94ef7d1744", "0xddfa070018382d1b55ffab424c70652226151166"),
    ("0xa5837eb75532c3431eb09d13c82d314a8cccba72", "0x34db5db28208adc35a4e737cfbb5470e0afeadca"),
    ("0xa6d659e8713b8639819d05a818581c0a16078351", "0x270aea068f4f89c755a82da6d13ce017b35d5101"),
    ("0xb1be6c5fa8838e8b13bfdf6e569f44d3af45b40d", "0x407c9ed3ebe9befc5d3444f88aead820037f1ec6"),
    ("0xb277d7d64a2cfb6d8c6b7e43a89b24556866463e", "0xaf2f442494ceed36da123da73da49516851babed"),
    ("0xb7ab37aec527485b966cd02bf71897288f5400d3", "0x5e49b3123c399a49112b595a58a8c1e0ea17f9de"),
    ("0xbc4845a6842c2f005d682ed3996c2ac2e2ada9e3", "0xd81d11de4b856fb4b967577e241cc95399f4db8e"),
    ("0xbcd80ed12ea8d9d19ceedc9cfa2475b763a6bc56", "0x7401b64a718ecfd45ba1cc9feb766e241c3f1a9d"),
    ("0xc19b31e47a83668ea29789bdcfa3fe7a0a5bfc3e", "0xb0394fd4ec89bb795b507dc869b2b79682a414d1"),
    ("0xc8a8c20da2fc8e46927c9030aa9d15786f89c97f", "0xc24e16a382ab27c8dbd97e77867f456b311caa60"),
    ("0xc995b428548db80f9948f1cce7642fb97b0e5e64", "0x49545a37ebbf838e8e76d7b077f7d5cc07c50222"),
    ("0xcc64d3cd2f9adb956ed1b323a2420644c78c4d10", "0x109e033f75da0a490988be46057b95cdbac51fde"),
    ("0xcd2aab3c77a94a3d7f78f02989097c15125741d1", "0xdaaa8f3ca0b2080ee0ecc95eeef54bdcc2d05472"),
    ("0xd7fdd2c93f7379b0d92d997a3a74169f0fd56a66", "0xf13f1ecef332e9fb1445408165aa65a13243de84"),
    ("0xd860a8c21bc3e9915df7e9fe3b72cfd953d5a483", "0x94f0ac15bcdc1e19f6066db203e0ec526003a095"),
    ("0xe60aa8a8d02b5ac2084aeafb980f972f5e2ec929", "0xfcc6b784e6330c021faaac4a8c03086b3d43cf77"),
    ("0xe99a050fd8f2c5d5bb70da5e67f4a5c5b4af8fdd", "0x0486f7b588898a763d4832cc56e6a2f27b3c849d"),
    ("0xf07e5a7ab86715e438d89019b28002b41c4f7833", "0x811827927e11c233566606bc6978c4b54774145d"),
]

CLUSTERS = {
    "main": {
        "cluster_id": "0x0eaced04ec017ea0d9985b6bcd16657b5b2dac78",
        "role": "MAIN",
        "n_tokens_total": 1641,
        "from_block": 24_592_957,
        "to_block": 34_788_433,
        "deployer_addresses": {
            "0x072e4e8a0e3463b00bf93f69a34db4874030819c", "0x0cfaa33eb4e786fca04b55e717ea9bdb291c62c6",
            "0x0eaced04ec017ea0d9985b6bcd16657b5b2dac78", "0x36cf37c0bdf51299a59c376790a360b2db66fdc0",
            "0x38f4fa33cf8292c3671f3bab1aa4d2cfff14f108", "0x39296166c0dcc770f30745dd2253c9b73dc1ec4a",
            "0x9361c7533216ca1d5ebe22a6b89351beb94a0357", "0x9e8477412ba258e0aff78b1a8590b1926dd92931",
            "0xc14a2637f27ca988633d58154cf4b35389f86700", "0xeaee48a727cd34a3b8c639bae643122e8900c8f2",
            "0xff39c17ec51a3c94411012314c90d572c6077312",
        },
        "sample": MAIN_SAMPLE,
    },
    "control": {
        "cluster_id": "0x376d633018680caa4ec3f3e735a2797abf7f9cb2",
        "role": "CONTROL",
        "n_tokens_total": 481,
        "from_block": 27_311_849,
        "to_block": 32_932_594,
        "deployer_addresses": {
            "0x376d633018680caa4ec3f3e735a2797abf7f9cb2", "0xa219966bd6217c06d13e5788386fed7e0ac77575",
        },
        "sample": CONTROL_SAMPLE,
    },
}


def _verify_pair_token_is_weth() -> str | None:
    """Один eth_call (symbol()) -- подтверждает допущение, что
    PAIR_TOKEN == WETH, вместо молчаливого предположения. Общий для
    обоих кластеров (тот же pair_token у всех 39680 V1-запусков)."""
    try:
        result = _rpc_call("eth_call", [{"to": PAIR_TOKEN, "data": "0x95d89b41"}, "latest"])
        raw = bytes.fromhex(result[2:])
        length = int.from_bytes(raw[32:64], "big")
        return raw[64 : 64 + length].decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 -- диагностика, не критично
        print(f"[sc1_wash_slice] symbol() на pair_token не удался (не блокирует прогон): {e}")
        return None


def _decode_swap_data(data_hex: str) -> tuple[int, int]:
    raw = bytes.fromhex(data_hex[2:])

    def to_signed(word: bytes) -> int:
        v = int.from_bytes(word, "big")
        return v - (1 << 256) if v >= (1 << 255) else v

    return to_signed(raw[0:32]), to_signed(raw[32:64])


def _fetch_logs_chunked(addresses: list[str], from_block: int, to_block: int, chunk_size: int = 2000):
    """Тонкая обёртка над общим `alchemy_fallback._chunked_get_logs`
    (ретрай на 429 + фолбэк на Alchemy/Blockscout при стойкой ошибке)."""
    n_calls = 0

    def _count(lo: int, hi: int, n_results: int) -> None:
        nonlocal n_calls
        n_calls += 1

    yield from _chunked_get_logs(
        from_block, to_block, [topic0(UNISWAP_V3_SWAP_SIG)],
        chunk_size=chunk_size, address=addresses, on_call=_count,
    )
    print(f"[sc1_wash_slice] фактическое число вызовов eth_getLogs: {n_calls}")


def run_cluster(key: str, spec: dict) -> dict:
    cluster_id = spec["cluster_id"]
    sample = spec["sample"]
    token_by_pool = {pool.lower(): token.lower() for token, pool in sample}
    addresses = list(token_by_pool.keys())
    deployer_set = {a.lower() for a in spec["deployer_addresses"]}

    print(f"\n[sc1_wash_slice] === {spec['role']} {cluster_id} "
          f"(выборка {len(sample)} из {spec['n_tokens_total']}) ===")

    wallet_pool_volume: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    token_buyers: dict[str, set[str]] = defaultdict(set)  # pool -> set(wallet), для медианы внешних покупателей
    total_volume_usd = 0.0
    n_logs = 0

    for log in _fetch_logs_chunked(addresses, spec["from_block"], spec["to_block"]):
        n_logs += 1
        pool = log["address"].lower()
        token = token_by_pool.get(pool)
        if token is None:
            continue
        recipient = "0x" + log["topics"][2][-40:]
        amount0, amount1 = _decode_swap_data(log["data"])
        weth_is_token0 = int(PAIR_TOKEN, 16) < int(token, 16)
        weth_amount_wei = amount0 if weth_is_token0 else amount1
        volume_usd = abs(weth_amount_wei) / 1e18 * ETH_USD_PRICE

        wallet_pool_volume[recipient][pool] += volume_usd
        total_volume_usd += volume_usd
        token_buyers[pool].add(recipient)

    n_wallets = len(wallet_pool_volume)

    # Метрика 1 (пре-регистрация): доля объёма кошельков-повторников
    # (>=5 РАЗНЫХ токенов -- в пределах ЭТОЙ выборки, см. докстринг).
    repeat_wallets_volume = 0.0
    n_repeat_wallets = 0
    for wallet, pools in wallet_pool_volume.items():
        if len(pools) >= 5:
            n_repeat_wallets += 1
            repeat_wallets_volume += sum(pools.values())
    share_repeat = (repeat_wallets_volume / total_volume_usd) if total_volume_usd > 0 else float("nan")
    if share_repeat > KILL_THRESHOLD:
        verdict = "KILL"
    elif share_repeat < MAIN_LINE_THRESHOLD:
        verdict = "главная линия"
    else:
        verdict = "третий кластер (неоднозначно, нужна сверка ещё на одном)"

    # Метрика 2: доля объёма у топ-3 адресов (по объёму, независимо от >=5 порога).
    wallet_totals = {w: sum(pools.values()) for w, pools in wallet_pool_volume.items()}
    top3_volume = sum(sorted(wallet_totals.values(), reverse=True)[:3])
    share_top3 = (top3_volume / total_volume_usd) if total_volume_usd > 0 else float("nan")

    # Метрика 3: медиана числа ВНЕШНИХ покупателей на токен (покупатель
    # != адрес-деплоер этого кластера, тот же принцип, что паспорт SC1
    # Шаг 3).
    external_buyers_per_token = []
    for pool in addresses:
        buyers = token_buyers.get(pool, set())
        external = buyers - deployer_set
        external_buyers_per_token.append(len(external))
    external_buyers_per_token.sort()
    n = len(external_buyers_per_token)
    median_external_buyers = (
        external_buyers_per_token[n // 2] if n % 2 == 1
        else (external_buyers_per_token[n // 2 - 1] + external_buyers_per_token[n // 2]) / 2
    )

    result = {
        "cluster_id": cluster_id,
        "role": spec["role"],
        "n_tokens_in_sample": len(sample),
        "n_tokens_total_in_cluster": spec["n_tokens_total"],
        "from_block": spec["from_block"],
        "to_block": spec["to_block"],
        "n_logs": n_logs,
        "total_volume_usd": total_volume_usd,
        "n_unique_wallets": n_wallets,
        "n_repeat_wallets_ge5_tokens_in_sample": n_repeat_wallets,
        "repeat_wallets_volume_usd": repeat_wallets_volume,
        "share_volume_from_repeat_wallets": share_repeat,
        "kill_threshold": KILL_THRESHOLD,
        "main_line_threshold": MAIN_LINE_THRESHOLD,
        "verdict": verdict,
        "share_volume_top3_wallets": share_top3,
        "median_external_buyers_per_token": median_external_buyers,
    }
    print(json.dumps(result, indent=2, default=str))
    return result


def run() -> int:
    symbol = _verify_pair_token_is_weth()
    print(f"[sc1_wash_slice] pair_token {PAIR_TOKEN} symbol() = {symbol!r} "
          f"({'подтверждено WETH' if symbol and symbol.upper() in ('WETH', 'ETH') else 'НЕ подтверждено как WETH'})")

    # НАЙДЕНО 2026-09-02 (run 33575241260, второй прогон): результат
    # писался ТОЛЬКО в конце, после ОБОИХ кластеров -- сбой (429 после
    # исчерпания ретраев) на CONTROL после уже готового MAIN терял и
    # MAIN тоже, несмотря на потраченное время. Теперь -- инкрементальная
    # запись после КАЖДОГО кластера (тот же принцип, что
    # data/credits_spent.json/sprint*_cache по всему проекту -- ничего
    # не теряется, если job оборвётся на середине).
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # НАЙДЕНО 2026-09-02 (третий прогон подряд): при ретрае после
    # частичного сбоя скрипт гнал MAIN заново с нуля (5228 вызовов,
    # ~35 минут), хотя результат уже лежал в закоммиченном OUT_PATH --
    # инкрементальная запись спасала от ПОТЕРИ результата, но не от
    # ПОВТОРНОЙ работы. Теперь -- resume: уже готовые кластеры (по ключу
    # в существующем файле) не пересчитываются.
    results = {}
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text())
            results = existing.get("clusters", {})
            if results:
                print(f"[sc1_wash_slice] resume: уже готовы кластеры {list(results.keys())} -- пересчитываться не будут")
        except (json.JSONDecodeError, OSError) as e:  # noqa: BLE001 -- битый/отсутствующий кэш -- начинаем с нуля, не падаем
            print(f"[sc1_wash_slice] не удалось прочитать существующий {OUT_PATH} ({e}) -- начинаю с нуля")
            results = {}

    for key, spec in CLUSTERS.items():
        if key in results:
            continue
        results[key] = run_cluster(key, spec)
        out = {
            "pair_token_symbol_verified": symbol,
            "random_seed": RANDOM_SEED,
            "clusters": results,
            "complete": set(results.keys()) == set(CLUSTERS.keys()),
        }
        OUT_PATH.write_text(json.dumps(out, indent=2, default=str))
        print(f"[sc1_wash_slice] промежуточно записано {OUT_PATH} после кластера {key!r}")

    print(f"\n[sc1_wash_slice] записано {OUT_PATH} (только агрегаты, оба кластера)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
