"""Общий модуль для работы с фабрикой `PonsV2LaunchFactory` (chain id
4663, Robinhood Chain) -- используется `sc1_v2_recon.py` (только чтение,
`eth_call`) и `sc1_launcher.py` (чтение + опционально подпись/отправка).

Источник всех сигнатур/структур -- `docs/SC1_LAUNCHER.md` (разведка по
первоисточнику, `contractsV2/src/v2/PonsV2LaunchFactory.sol` и
`PonsV2LauncherToken.sol` в `ponsdotdev/ponsfamily`, дословные запросы
2026-09-02). НЕ угадано -- см. докстринг `sc1_v2_recon.py` для деталей
верификации.

Никаких приватных ключей/подписи здесь -- этот модуль только строит
calldata и декодирует ответы. Подпись -- отдельно, в sc1_launcher.py,
только там, где явно запрошена реальная отправка.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eth_abi import encode as abi_encode, decode as abi_decode

from alchemy_fallback import _rpc_call, topic0  # noqa: E402

V2_FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"

# Кандидат pairToken -- тот же адрес, что PAIR_TOKEN во всех 39680
# V1-запусках (data/sprintSC1_cache, sc1_wash_slice.py), symbol()
# подтверждён как WETH прогоном wash-slice. Для V2 это была ГИПОТЕЗА --
# ОПРОВЕРГНУТА живым recon (2026-09-02, run 33575904035):
# approvedPairTokens[CANDIDATE_WETH] = False. Оставлен для справки/
# диагностики, НЕ используется как дефолт -- см. NATIVE_PAIR_TOKEN.
CANDIDATE_WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"

# Дефолтный путь V2 -- нативный ETH как quote-актив. Источник
# (дословный запрос _launchToken): `if (pairToken != address(0) &&
# !approvedPairTokens[pairToken]) revert PairTokenNotApproved();` --
# address(0) ЦЕЛИКОМ ОБХОДИТ проверку approvedPairTokens, и
# `previewLaunchEconomics`/economics digest в этом случае берёт
# `config.phantomQuote`/`config.graduationThreshold` НАПРЯМУЮ (не из
# `pairTokenEconomics[pairToken]`). Подтверждено live-значениями
# конфига 0 (2026-09-02): phantomQuote=1.68e18, graduationThreshold=
# 4.2e18 -- правдоподобные ETH-величины (1.68 / 4.2 ETH), согласуется
# с нативным ETH как quote. `buy()`/`sell()` на кривой -- `payable`,
# тоже согласуется с нативным ETH, не ERC20.
NATIVE_PAIR_TOKEN = "0x0000000000000000000000000000000000000000"


def _selector(signature: str) -> bytes:
    return bytes.fromhex(topic0(signature)[2:10])


SOCIALS_TYPE = "(string,string,string,string,string)"
TOKEN_PARAMS_TYPE = f"(string,string,string,string,{SOCIALS_TYPE},address,uint16,bool,bytes32,bytes32)"


@dataclass(frozen=True)
class Socials:
    twitter: str = ""
    telegram: str = ""
    discord: str = ""
    website: str = ""
    farcaster: str = ""

    def as_tuple(self) -> tuple:
        return (self.twitter, self.telegram, self.discord, self.website, self.farcaster)


@dataclass(frozen=True)
class TokenParams:
    name: str
    symbol: str
    logo: str
    description: str
    socials: Socials
    creator_fee_recipient: str
    creator_tax_bps: int
    buyback_enabled: bool
    expected_economics: bytes  # 32 bytes
    salt: bytes  # 32 bytes

    def as_tuple(self) -> tuple:
        return (
            self.name, self.symbol, self.logo, self.description,
            self.socials.as_tuple(), self.creator_fee_recipient,
            self.creator_tax_bps, self.buyback_enabled,
            self.expected_economics, self.salt,
        )


@dataclass(frozen=True)
class LaunchConfig:
    supply: int
    curve_fee_bps: int
    phantom_quote: int
    graduation_threshold: int
    pool_fee: int
    tick_spacing: int
    enabled: bool


def _eth_call(data: bytes) -> bytes:
    result = _rpc_call("eth_call", [{"to": V2_FACTORY, "data": "0x" + data.hex()}, "latest"])
    return bytes.fromhex(result[2:])


def launch_fee() -> int:
    raw = _eth_call(_selector("launchFee()"))
    return abi_decode(["uint256"], raw)[0]


def launch_enabled() -> bool:
    raw = _eth_call(_selector("launchEnabled()"))
    return abi_decode(["bool"], raw)[0]


def can_launch(launcher: str) -> bool:
    data = _selector("canLaunch(address)") + abi_encode(["address"], [launcher])
    return abi_decode(["bool"], _eth_call(data))[0]


def launch_config_count() -> int:
    raw = _eth_call(_selector("launchConfigCount()"))
    return abi_decode(["uint256"], raw)[0]


def get_launch_config(config_id: int) -> LaunchConfig:
    data = _selector("getLaunchConfig(uint256)") + abi_encode(["uint256"], [config_id])
    raw = _eth_call(data)
    supply, curve_fee_bps, phantom_quote, graduation_threshold, pool_fee, tick_spacing, enabled = abi_decode(
        ["uint256", "uint256", "uint256", "uint256", "uint24", "int24", "bool"], raw
    )
    return LaunchConfig(supply, curve_fee_bps, phantom_quote, graduation_threshold, pool_fee, tick_spacing, enabled)


def approved_pair_tokens(pair_token: str) -> bool:
    data = _selector("approvedPairTokens(address)") + abi_encode(["address"], [pair_token])
    return abi_decode(["bool"], _eth_call(data))[0]


def max_creator_tax_bps() -> int:
    raw = _eth_call(_selector("maxCreatorTaxBps()"))
    return abi_decode(["uint256"], raw)[0]


def whitelisted_launchers(launcher: str) -> bool:
    data = _selector("whitelistedLaunchers(address)") + abi_encode(["address"], [launcher])
    return abi_decode(["bool"], _eth_call(data))[0]


def preview_launch_economics(launch_config_id: int, pair_token: str) -> bytes:
    data = _selector("previewLaunchEconomics(uint256,address)") + abi_encode(
        ["uint256", "address"], [launch_config_id, pair_token]
    )
    raw = _eth_call(data)
    return abi_decode(["bytes32"], raw)[0]


def build_launch_calldata(params: TokenParams, launch_config_id: int, pair_token: str) -> bytes:
    """launchToken(TokenParams,uint256,address) -- 2-параметровая
    перегрузка (без snipeTaxExemptions), см. docs/SC1_LAUNCHER.md."""
    signature = f"launchToken({TOKEN_PARAMS_TYPE},uint256,address)"
    selector = _selector(signature)
    encoded = abi_encode(
        [TOKEN_PARAMS_TYPE, "uint256", "address"],
        [params.as_tuple(), launch_config_id, pair_token],
    )
    return selector + encoded


def eth_estimate_gas(from_addr: str, calldata: bytes, value_wei: int) -> int:
    tx = {
        "from": from_addr,
        "to": V2_FACTORY,
        "data": "0x" + calldata.hex(),
        "value": hex(value_wei),
    }
    result = _rpc_call("eth_estimateGas", [tx])
    return int(result, 16)


def eth_gas_price() -> int:
    result = _rpc_call("eth_gasPrice", [])
    return int(result, 16)


def eth_get_transaction_count(addr: str, block: str = "pending") -> int:
    result = _rpc_call("eth_getTransactionCount", [addr, block])
    return int(result, 16)


def eth_chain_id() -> int:
    result = _rpc_call("eth_chainId", [])
    return int(result, 16)
