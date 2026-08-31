-- 04_sniper_insider_exclusions.sql
-- Гейт 1: список адресов-снайперов/инсайдеров, подлежащих исключению из
-- ОБЕИХ когорт (А и Б).
--
-- Критерий 1 (same-block / early-block снайпинг): первый своп кошелька в
-- конкретном пуле произошёл в блоке создания пула или в пределах
-- {{sniper_block_window}} блоков после (см. sql/01_pool_creation_blocks.sql).
--
-- Критерий 2 (связь с деплойером): кошелёк получил прямой перевод
-- (ETH или ЛЮБОЙ токен) от deployer_address ДО своего первого свопа в
-- этом пуле.

with pools as (
    select * from query_01_pool_creation_blocks
),

swaps as (
    select * from query_02_swaps_raw_july
),

first_swap_per_wallet_pool as (
    select
        wallet_address,
        pool_address,
        min(block_number) as first_swap_block,
        min(block_time) as first_swap_time
    from swaps
    group by 1, 2
),

-- Критерий 1
early_block_snipers as (
    select distinct f.wallet_address
    from first_swap_per_wallet_pool f
    join pools p on p.pool_address = f.pool_address
    where f.first_swap_block <= p.sniper_window_end_block
),

-- Критерий 2: любой transfer (ETH или ERC20) deployer -> wallet, случившийся
-- строго до первого свопа кошелька в пуле, задеплоенном этим deployer'ом.
deployer_funded_wallets as (
    select distinct f.wallet_address
    from first_swap_per_wallet_pool f
    join pools p on p.pool_address = f.pool_address
    join robinhood.traces tr
        on tr."to" = f.wallet_address
       and tr."from" = p.deployer_address
       and tr.block_time < f.first_swap_time
       and tr.value > uint256 '0'
    where tr."from" != f.wallet_address  -- деплойер не сам себе шлёт

    union

    select distinct f.wallet_address
    from first_swap_per_wallet_pool f
    join pools p on p.pool_address = f.pool_address
    join erc20_robinhood.evt_transfer erc
        on erc."to" = f.wallet_address
       and erc."from" = p.deployer_address
       and erc.evt_block_time < f.first_swap_time
)

select wallet_address, 'early_block_sniper' as reason from early_block_snipers
union
select wallet_address, 'deployer_funded' as reason from deployer_funded_wallets
