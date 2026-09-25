> For the complete documentation index, see [llms.txt](https://docs.triton.one/llms.txt). Markdown versions of documentation pages are available by appending `.md` to page URLs; this page is available as [Markdown](https://docs.triton.one/chains/solana/preconfirmations-grpc.md).

# Preconfirmations gRPC

Triton Preconfs streams preconfirmed Solana transactions over gRPC, from the two block production systems that emit them: Harmonic and BAM.

### What a preconfirmation is

On Solana one validator is the [leader](https://docs.anza.xyz/consensus/leader-rotation) for each slot and builds the block for it. A transaction you send travels to that leader; the leader executes it, packs it into entries, splits the entries into shreds and broadcasts them; every other node receives the shreds, replays the block and only then reports the transaction, first at `processed` [commitment](https://solana.com/docs/rpc#configuring-state-commitment), later at `confirmed` and `finalized`. Everything you can observe through an RPC node, a WebSocket or a Geyser stream sits at the end of that path.

A preconfirmation is a message emitted at the start of it. The party assembling the block, the builder or the leader itself, tells you the moment a transaction has been executed or committed into the slot under construction, before any shred exists. You learn what the block will contain while it is still being built. The message carries the raw transaction bytes exactly as the block will, plus the slot and, depending on the feed, the execution outcome and the position in the block.

A preconfirmation is a statement by the block producer, not a confirmation by the cluster. The slot can still be skipped, the block can land on a fork that is abandoned, and a builder can restart a slot. A preconfirmed transaction almost always lands, but "almost" is the operative word: act on it as the earliest possible signal, and treat `confirmed` and `finalized` as the settlement they are.

### How each feed produces one

[**Harmonic**](https://docs.harmonic.gg/) runs block builders next to leaders that run the Harmonic validator client. Transactions reach the builder, which executes them and fixes their order for the block. As it does so it streams the executed transactions to subscribers in batches, numbered from zero within each slot and delivered in order, with a start and an end marker per slot. Each transaction carries the outcome the builder observed: success, execution failure (committed, fees charged, state reverted) or fees only (failed to load, only the fee is charged). The leader then executes the same transactions in the same order; Harmonic guarantees the resulting state is equivalent to executing them serially in batch order, so the outcome you receive is the outcome that lands. A Harmonic preconfirmation therefore means: this transaction was executed with this result and sits in the block being built.

[**BAM**](https://bam.dev/docs/bam/bam-overview/), Jito's Block Assembly Marketplace, sits between transaction senders and leaders that run the BAM validator client. A BAM node schedules the transactions it receives, first in first out with respect to the accounts they lock, and hands them to the leader; the leader executes them and acknowledges each one back to the node, which streams it to subscribers. Each transaction carries the node it came through, its position in the scheduler's sequence (transactions sharing a sequence were bundled together), its position inside that bundle, and whether the bundle reverts on error. BAM does not report an execution outcome: a BAM preconfirmation means this transaction was committed by the leader into the slot; whether it succeeded is only visible once the block lands.

Triton subscribes to both systems in every region they publish from and delivers the same bytes to you over one endpoint, filtered to the accounts and signatures you ask for.

#### Harmonic and BAM side by side

|                          | Harmonic                                             | BAM                                                      |
| ------------------------ | ---------------------------------------------------- | -------------------------------------------------------- |
| who produces the preconf | the block builder, before the leader executes        | the BAM node, after the leader executes and acknowledges |
| what it asserts          | executed with this outcome, in the block being built | committed by the leader into the slot                    |
| execution outcome        | success, execution failure, fees only                | not reported                                             |
| ordering information     | batch number within the slot                         | scheduler sequence, bundle position, revert on error     |
| slot framing             | `SlotStart` and `SlotEnd` per slot                   | none, each transaction names its slot                    |
| regions                  | 7                                                    | 15                                                       |

### What the feeds cover, and what they do not

The questions we get most often come from expecting the feeds to be a copy of the chain. They are not. Three things decide what a stream carries.

**Only slots led by a validator running that client.** Harmonic preconfirms the slots of leaders on the Harmonic client; BAM preconfirms the slots of leaders on the BAM client. A slot led by any other validator has no preconfirmation on either feed, from any region. The share of slots each feed covers follows the stake running its client and changes as validators switch. Neither feed sees the other's slots, so subscribing to both is how you cover the most slots.

**Static account keys only.** Filters match the accounts written in the transaction message. Accounts a v0 transaction reaches through an address lookup table are not matched; legacy and v1 transactions carry every account inline and are matched in full. This is the single most common reason a filter sees fewer transactions than expected; see Address lookup tables below.

**One region, one stream, and only the regions near you.** Each regional stream carries the slots of the leaders that region serves, not a copy of the whole feed. A far region's preconfirmations arrive after the people located there have acted on them. See Choosing regions.

### Endpoint and access

`https://preconfs.rpcpool.com`

The address is anycast: the connection lands on the closest point of presence, one of the servers behind that address; every point of presence serves every region of both feeds.

#### Token

Every request, `GetVersion` included, carries an `x-token`: the token of a subscription that has Harmonic preconfs, BAM preconfs or both enabled. It is the same token you use for other Triton gRPC services on that subscription. Send the token's value, not its name.

Enabling a feed on a subscription reaches the servers within about a minute. Until then, and for a token whose subscription does not have that feed, subscribing is refused with `UNAUTHENTICATED` and the message `x-token not entitled to this feed`. Turning a feed off closes the open streams of that token with `PERMISSION_DENIED`.

#### Which point of presence answered

`GetVersion` returns the server version and the point of presence that took your connection, and needs the token like every other call:

```
grpcurl -import-path . -proto preconfs.proto -H "x-token: $TOKEN" \
  preconfs.rpcpool.com:443 preconfs.Harmonic/GetVersion
{
  "version": "0.1.13",
  "region": "fra1"
}
```

The same call is `Client::version()` in the Rust client.

#### Client

The Rust client and the protobuf definitions are on [GitHub](https://github.com/rpcpool/preconfs-client) as the `triton-preconfs-client` and `triton-preconfs-proto` crates. Other languages use the proto directly, see Other languages.

### Quick start

Add the client:

```toml
[dependencies]
triton-preconfs-client = "0.1"
tokio = { version = "1", features = ["rt-multi-thread", "macros"] }
```

Subscribe to Harmonic in Amsterdam for transactions touching the SPL token program and print what arrives:

```rust
use solana_pubkey::Pubkey;
use triton_preconfs_client::{Connector, Event, Feed, Filter, Filters, Region, parse};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = Connector::new("https://preconfs.rpcpool.com")
        .x_token(Some(std::env::var("PRECONFS_TOKEN")?))
        .connect()
        .await?;

    let token_program: Pubkey = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA".parse()?;
    let region = Region::parse(Feed::Harmonic, "ams")?;
    let filters = Filters::single(Filter::new().accounts([token_program]));

    let mut stream = client.subscribe_harmonic(region, filters).await?;
    while let Some(event) = stream.next().await {
        match event? {
            Event::Transaction(matched) => {
                let signature = parse::parse_signature(&matched.transaction.transaction)?;
                println!("slot {} {signature}", matched.transaction.slot);
            }
            Event::SlotEnd { slot } => println!("slot {slot} complete"),
            Event::Reconnected { attempts } => println!("reconnected after {attempts} attempts"),
            _ => {}
        }
    }
    Ok(())
}
```

The same shape works for BAM with `Feed::Bam` and `subscribe_bam`.

#### The example CLI

The repository ships `preconfs-subscribe`, which subscribes and logs every event:

```
cargo run -p preconfs-example -- --x-token $TOKEN --region harmonic:ams \
    --account TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA
```

`--region bam:fra`, `--require`, `--signature`, `--result` and `--no-reconnect` cover the other options; `--help` lists them.

#### Trying it with grpcurl

With `preconfs.proto` from the repository in the current directory:

```
grpcurl -import-path . -proto preconfs.proto \
  -H "x-token: $TOKEN" \
  -d '{"harmonic_region":"HARMONIC_REGION_FRA","transactions":{"t":{"account_include":["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"]}}}' \
  preconfs.rpcpool.com:443 preconfs.Harmonic/Subscribe
```

For BAM call `preconfs.BAM/Subscribe` with `"bam_region":"BAM_REGION_FRA"`.

### Feeds and regions

Three things carry the word region; they are not the same thing.

```
Solana leaders                        Solana leaders
      │                                     │
      ▼                                     ▼
Harmonic builders                       BAM nodes
ams ewr fra lon tyo sgp slc             ams dfw dub ewr fra hkg iad lax
                                        lon pit sea sin slc sqq tyo
      │                                     │
      └─────────────────┬───────────────────┘
                        ▼
          Triton points of presence
          ams1 fra1 lon1 nyc1 ...   each relays every region
                        │
                        ▼
          https://preconfs.rpcpool.com   anycast picks the closest
                        │
                        ▼
          your stream: one feed, one region
```

**Feed regions** are the ones in the proto (`HarmonicRegion`, `BamRegion`) and in the subscribe request. Harmonic runs a block builder in each of its regions and BAM runs a node in each of its; a leader is served by one of them, and that location's stream carries the slots of the leaders it serves. The regional streams are not copies of each other: `harmonic:ams` and `harmonic:fra` deliver different slots. Subscribe to the ones near your servers; see Choosing regions.

**Origin on each transaction**: `region` on a Harmonic transaction and `node` on a BAM transaction name the builder or node it came through. On a single region stream it matches what you subscribed; it is there so transactions stay self describing when you merge streams.

**Points of presence** are Triton's servers, named after their site (`ams1`, `fra1`, `lon1`, `nyc1` and so on). The anycast address lands you on the closest one and every point of presence relays every feed region, so where you connect never limits what you can subscribe to. `Client::version` returns the one that answered in its `region` field.

A stream serves one feed in one region. A region of the other feed is refused.

#### Harmonic

The builder executes the transaction and reports the outcome. Streams are framed per slot (see The stream) and carry an execution result on every transaction, so `execution_results` filters are accepted.

| region | location          |
| ------ | ----------------- |
| `ams`  | Amsterdam         |
| `ewr`  | New York (Newark) |
| `fra`  | Frankfurt         |
| `lon`  | London            |
| `tyo`  | Tokyo             |
| `sgp`  | Singapore         |
| `slc`  | Salt Lake City    |

#### BAM

The leader commits the transaction; no outcome is reported and there is no slot framing. Each transaction names its slot.

| region | location             |
| ------ | -------------------- |
| `ams`  | Amsterdam            |
| `dfw`  | Dallas               |
| `dub`  | Dublin               |
| `ewr`  | New York (Newark)    |
| `fra`  | Frankfurt            |
| `hkg`  | Hong Kong            |
| `iad`  | Washington (Ashburn) |
| `lax`  | Los Angeles          |
| `lon`  | London               |
| `pit`  | Pittsburgh           |
| `sea`  | Seattle              |
| `sin`  | Singapore            |
| `slc`  | Salt Lake City       |
| `sqq`  | Siauliai             |
| `tyo`  | Tokyo                |

#### Choosing regions

Pick the regions near you. Do not subscribe to all of them.

A preconfirmation is worth what its lead over the landed block is worth, and that lead is spent on the wire. Cables do not run in straight lines: a packet from Tokyo to Frankfurt takes around 250 ms on a normal route. A slot preconfirmed in Tokyo therefore reaches a server in Frankfurt a quarter of a second after it reached a server in Tokyo. By then a trader sitting in Tokyo has acted on it, and the block itself has long been broadcast. For a far region you pay for the stream and still come second every time. This is not a limit we impose; it is geography, and it is the reason the feeds publish per region in the first place.

So: subscribe to the regions whose builders and nodes sit in your own data centre or metro, one stream per region. Someone in Frankfurt subscribes to `harmonic:fra` and `bam:fra`, maybe `ams` and `lon` next to them; someone in New York to `ewr`, maybe `iad` and `pit`. Regions elsewhere only make sense for a server placed there. The streams of the regions you pick share one connection.

If you do open several, merge them. Each stream carries different slots, so there is nothing to deduplicate between regions of the same feed, and framing is per stream: a `SlotEnd` on `ams` says nothing about `fra`. In Rust, `select_all` polls whichever region has an event ready:

```rust
use futures::stream::{StreamExt, select_all};
use solana_pubkey::Pubkey;
use triton_preconfs_client::{Connector, Event, Feed, Filter, Filters, Region, parse};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = Connector::new("https://preconfs.rpcpool.com")
        .x_token(Some(std::env::var("PRECONFS_TOKEN")?))
        .connect()
        .await?;

    let token_program: Pubkey = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA".parse()?;
    let filter = Filter::new().accounts([token_program]);

    // The regions next to this server, one stream each, on one connection,
    // tagged with the region name so the merged output stays self describing.
    let mut streams = Vec::new();
    for name in ["fra", "ams", "lon"] {
        let region = Region::parse(Feed::Harmonic, name)?;
        let stream = client
            .subscribe_harmonic(region, Filters::single(filter.clone()))
            .await?;
        streams.push(stream.map(move |event| (name, event)).boxed());
    }

    let mut merged = select_all(streams);
    while let Some((region, event)) = merged.next().await {
        match event? {
            Event::Transaction(matched) => {
                let signature = parse::parse_signature(&matched.transaction.transaction)?;
                println!("{region} slot {} {signature}", matched.transaction.slot);
            }
            Event::SlotEnd { slot } => println!("{region} slot {slot} complete"),
            Event::Clip { transactions } => println!("{region} clipped {transactions}"),
            Event::Reconnected { attempts } => println!("{region} reconnected after {attempts}"),
            Event::SlotStart { .. } => {}
        }
    }
    Ok(())
}
```

`futures = "0.3"` provides `select_all`. The same shape with `Feed::Bam` and `subscribe_bam` covers the BAM regions near you; the two feeds have different event types, so merge them separately or map both into your own type first. Each stream reconnects on its own, so a point of presence restart shows up as one `Reconnected` per region.

Two more things to know when you open several streams:

* They all draw on your account's coverage share (see Coverage). A wide filter across many regions reaches the share quickly; the share is meant for filters that select what you actually use.
* The concurrent stream limit is 1000 per account, far above what any placement needs.

#### In the client

```rust
let region = Region::parse(Feed::Harmonic, "ams")?;
let region: Region = "bam:fra".parse()?;
Feed::Bam.regions(); // the names above
```

To pin one point of presence instead of letting anycast choose, `Connector::dial("host:port")` opens the TCP connection to that address while TLS keeps the endpoint's host name. `Client::version` returns the server version and the region of the point of presence answering.

### Filters

A subscribe request carries one or more named filters. A transaction is delivered when it matches at least one, and every delivered transaction names the filters it matched.

A transaction matches a filter when it satisfies every condition the filter sets:

| condition           | matches when the transaction                     |
| ------------------- | ------------------------------------------------ |
| `account_include`   | references any of these accounts                 |
| `account_required`  | references all of these accounts                 |
| `signatures`        | has one of these first signatures                |
| `execution_results` | landed with one of these outcomes, Harmonic only |

Every filter must set at least one of the first three; the full feed cannot be subscribed.

"References" means the account is a static account key of the transaction message. Accounts loaded through address lookup tables do not count, see What the feeds cover.

#### account\_exclude

There is no `account_exclude` today. Filters are positive selections: every filter names the accounts or signatures it wants, so a subscription cannot ask for the whole feed minus a few accounts. To narrow a selection, combine `account_include` with `account_required` (the transaction must reference all of these), or on Harmonic with `execution_results`, and drop what you do not want on your side.

The proto reserves the field for it, and `account_exclude` as a further condition on a positive filter (include A, minus B) is planned. It will not allow subscribing to everything except some accounts.

#### Limits

| limit                                                     | value    |
| --------------------------------------------------------- | -------- |
| filters per stream                                        | 64       |
| accounts per `account_include` or `account_required` list | 10000    |
| signatures per filter                                     | 1000     |
| filter name                                               | 64 bytes |
| concurrent streams per account                            | 1000     |

The client checks the first four before sending, so a request over a limit fails locally with a `FilterError`.

#### In the client

```rust
let filters = Filters::new()
    .with("token", Filter::new().accounts([token_program]))
    .with("mine", Filter::new().accounts([token_program]).require([my_account]))
    .with("landed", Filter::new().accounts([my_account]).execution_results([ExecutionResult::Success]));
```

`Filters::single(filter)` names a lone filter `default`.

### Address lookup tables

Solana has three transaction message formats, and only one of them hides accounts:

| format                                                                                                                                                 | accounts                                                                                                                           | lookup tables |
| ------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| legacy                                                                                                                                                 | every account written in the message                                                                                               | no            |
| v0                                                                                                                                                     | static accounts in the message, plus indexes into on-chain [address lookup tables](https://solana.com/docs/advanced/lookup-tables) | yes           |
| v1 ([SIMD-0385](https://github.com/solana-foundation/solana-improvement-documents/blob/main/proposals/0385-v1-transaction-format.md), the 4 KB format) | every account written in the message, up to 64                                                                                     | no            |

For legacy and v1 transactions the bytes name every account, so a filter sees all of them. The gap is v0: its bytes carry only the table address and an index for each looked-up account, and turning those into addresses means reading the table's current state on chain.

Preconfirmations are the raw transaction bytes, emitted before the block exists. Neither Harmonic nor BAM resolves v0 lookup tables before streaming, and Triton does not resolve them on the way through: it would mean an account lookup per transaction on the hot path, and the whole point of the feed is the milliseconds it is ahead. So for v0 transactions, filters match the static keys only. The client parses all three formats; `parse::parse_static_parts` returns exactly the keys a filter sees.

What this means in practice:

* Program ids and well known accounts are almost always static keys, in every format. A filter on the SPL token program, a DEX program or pump.fun matches every transaction that invokes it.
* Pools, positions, vaults and user token accounts in v0 transactions are often loaded through tables, especially from aggregators and bots. A filter on such an account misses the v0 transactions that reach it through a table. Legacy and v1 transactions touching the same account match normally.
* The same v0 transaction, seen through Geyser or Deshred after the block lands, has its tables resolved. That is why a side by side comparison of "transactions touching account X" shows more on the landed stream than on the preconf stream.

If your accounts are mostly behind tables, filter on the program and on the static accounts that always accompany your transactions, and select the rest on your side. Both feed operators have said they intend to add resolution upstream, where it costs no latency; when they do, filters will match resolved accounts without any change on your side. As v1 adoption grows the gap closes on its own, since v1 has no tables.

### The stream

`subscribe_harmonic` and `subscribe_bam` return a stream of `Event`s.

| event                      | meaning                                                                                                                     |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `SlotStart { slot }`       | a leader began streaming preconfs for this slot (Harmonic)                                                                  |
| `Transaction(matched)`     | a matching transaction; `matched.filters` names the filters, `matched.transaction` is the feed's message with the raw bytes |
| `SlotEnd { slot }`         | no further transactions for this slot will arrive (Harmonic)                                                                |
| `Clip { transactions }`    | matching transactions were withheld, see Coverage                                                                           |
| `Reconnected { attempts }` | the stream dropped and was resubscribed; the data produced in between is gone                                               |

Pings are consumed by the stream.

#### Slot framing

On Harmonic every transaction sits between its slot's `SlotStart` and `SlotEnd`. After `SlotEnd` for a slot you hold everything your filters matched for it. A stream that subscribes while a slot is open joins at the next `SlotStart`, so the first slot you see is always complete. Slot boundaries arrive whether or not your filters matched anything in that slot.

Rarely a leader restarts a slot: a second `SlotStart` for a slot that already ended, followed by its definitive transactions and a new `SlotEnd`. Rebuild your view of that slot from the new frame; the last `SlotEnd` wins.

BAM has no framing. Each transaction names its slot.

#### Transaction bytes

`matched.transaction.transaction` holds the raw transaction bytes. The client parses what filtering needs without a full decode:

```rust
let signature = parse::parse_signature(&bytes)?;
let (signature, account_keys) = parse::parse_static_parts(&bytes)?;
```

Legacy, v0 and v1 message formats are supported.

#### Transaction fields

Harmonic (`HarmonicTransaction`):

| field         | meaning                                                               |
| ------------- | --------------------------------------------------------------------- |
| `transaction` | raw transaction bytes                                                 |
| `slot`        | slot the transaction was preconfirmed in                              |
| `result`      | the builder's outcome: success, execution failure or fees only        |
| `region`      | Harmonic region it was received from                                  |
| `seq`         | preconf batch within the slot, numbered from 0 and delivered in order |

BAM (`BamTransaction`):

| field                | meaning                                                                             |
| -------------------- | ----------------------------------------------------------------------------------- |
| `transaction`        | raw transaction bytes                                                               |
| `slot`               | slot the transaction was preconfirmed in                                            |
| `node`               | BAM node it was received from                                                       |
| `sequence`           | scheduler order on that node; transactions sharing a sequence were bundled together |
| `bundle_position`    | execution order within that bundle                                                  |
| `is_revert_on_error` | the bundle reverts on error (bundle intent, not an outcome)                         |

#### Slow consumers

A stream that cannot keep up ends with an explicit `RESOURCE_EXHAUSTED` status. Nothing is ever thinned silently: if you hold the stream open, it is complete for your filters, minus what `Clip` announces.

### Coverage

Each account may receive up to a share of a feed's traffic. This is a condition of the feeds: Harmonic and BAM license preconfirmations for your own use, and the share is what makes the full firehose impossible to obtain or resell through Triton. It is not a rate limit on the server; a narrow filter never hits it.

How it is measured:

* The share is **25%** of what a feed publishes in the region you subscribe, counted over a sliding **60 second** window. Harmonic and BAM are counted separately. The percentage is agreed with the feed operators and can change; this page states the current value.
* It belongs to the **account**, meaning your subscription: every token, connection and stream of the subscription draws on the same share. Opening more streams or rotating filters does not widen it.
* A **burst cap** holds instantaneous delivery to half the feed's current rate, so a spike cannot take the whole window's share in a second.
* Each point of presence measures on its own.

What happens over the share:

1. Matching transactions are withheld. The count arrives as a `Clip` event at most once a second, and on Harmonic always before the affected slot's `SlotEnd`, so a completed slot is never silently short.
2. If the account stays over the share for **30 seconds** without recovering, the stream ends with `RESOURCE_EXHAUSTED` and the message `account exceeded its coverage limit, cooling off`.
3. Subscribing again is refused for **60 seconds**, with the remaining time in the error message. The client's reconnect waits and retries on its own.

Staying under it: subscribe to the accounts you act on rather than to whole programs. A filter on the SPL token program or pump.fun across every region asks for most of the chain and reaches the share within a minute; the same accounts narrowed with `account_required` to your pools or wallets do not. If your use needs a larger share, ask through support; the share can be set per account.

### Billing

Two quantities are counted per subscription and per feed:

| quantity   | counts                                                                                           |
| ---------- | ------------------------------------------------------------------------------------------------ |
| `messages` | transactions delivered on your streams                                                           |
| `slots`    | slots your streams were open for, on Harmonic, whether or not a transaction matched your filters |

Harmonic is priced at the greater of a per slot and a per message amount, so the slot count is a floor: a stream open on a Harmonic region pays for every slot that region produces while it is open, matched or empty. BAM is priced per message only.

Not counted as messages: `SlotStart`, `SlotEnd`, pings, `Clip` notices, transactions your filters did not match, and transactions withheld by coverage.

Streams add up. Two streams whose filters overlap receive, and are counted for, the same transactions twice, and two streams on the same Harmonic region are two streams open on those slots. A transaction matching several filters on one stream is delivered and counted once; reconnecting within a slot does not count the slot again on the same point of presence.

Prices per message and per slot for each feed are on your subscription in the customer panel, together with the daily usage.

### Errors and reconnect

#### Error types

| type             | from                                  | when                                                                                                                                               |
| ---------------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ConnectError`   | `Connector::connect`, `connect_lazy`  | bad endpoint URI, token not ascii, TLS setup, connection refused or timed out                                                                      |
| `SubscribeError` | `subscribe_harmonic`, `subscribe_bam` | region of the wrong feed, filters over a limit, or the server refused the subscribe (bad token, region not served, feed not entitled, cooling off) |
| `StreamError`    | a stream item                         | the server ended the stream with a status, or closed it                                                                                            |

`RegionError`, `FilterError` and `ParseError` are the smaller types behind them. For one top level type wrap them with `anyhow` or `Box<dyn Error>`.

#### Status codes

| status                | meaning                                                                                     | what to do                                               |
| --------------------- | ------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `UNAUTHENTICATED`     | no `x-token`, unknown token, or the subscription does not have this feed enabled            | check the token value and that the feed is enabled       |
| `PERMISSION_DENIED`   | the feed was disabled on the subscription while the stream was open                         | the stream is closed; re-enable the feed                 |
| `INVALID_ARGUMENT`    | region missing, unspecified or of the other feed; a filter over a limit or with no selector | fix the request                                          |
| `FAILED_PRECONDITION` | the region is not served by this deployment                                                 | use one of the regions listed above                      |
| `RESOURCE_EXHAUSTED`  | over the coverage share for 30 seconds, still cooling off, or the client cannot keep up     | narrow the filters, read faster, retry after the cooloff |
| `DATA_LOSS`           | the client fell so far behind that the server dropped its position                          | reconnect; the gap is gone                               |
| `UNAVAILABLE`         | the point of presence is restarting                                                         | reconnect; anycast lands on another                      |

#### Reconnect

Points of presence restart on every deploy, so a long lived stream will drop. By default the stream resubscribes with the same request after a backoff and yields `Event::Reconnected`. Preconfs produced in between are gone; on Harmonic, framing restarts at the next `SlotStart`.

Errors that retrying cannot fix end the stream instead: `Unauthenticated`, `PermissionDenied`, `InvalidArgument`, `FailedPrecondition`, `NotFound`, `Unimplemented`. `Unavailable`, `DataLoss`, `ResourceExhausted`, `Internal`, `Aborted`, `DeadlineExceeded` and a closed stream are retried.

The default schedule waits 100ms, then doubles up to 10s between attempts, and never gives up. Tune or disable it on the connector:

```rust
use std::time::Duration;
use triton_preconfs_client::{Connector, Reconnect};

Connector::new(endpoint).reconnect(Reconnect {
    initial_interval: Duration::from_millis(250),
    multiplier: 2.0,
    max_interval: Duration::from_secs(30),
    max_retries: Some(20),
});

Connector::new(endpoint).no_reconnect();
```

With reconnect off the stream yields the error and ends.

### Other languages

The wire contract is `preconfs.proto` in the repository (`preconfs-proto/proto/preconfs.proto`); the same file is exported by the Rust proto crate as `PROTO_SOURCE`. Generate a client for your language from it.

#### Calls

| service                 | rpc                                                           |                                                              |
| ----------------------- | ------------------------------------------------------------- | ------------------------------------------------------------ |
| `preconfs.Harmonic`     | `Subscribe(SubscribeRequest) returns (stream HarmonicUpdate)` | the Harmonic stream                                          |
| `preconfs.Harmonic`     | `GetVersion(VersionRequest) returns (VersionResponse)`        | server version and point of presence region; needs the token |
| `preconfs.BAM`          | `Subscribe(SubscribeRequest) returns (stream BamUpdate)`      | the BAM stream                                               |
| `preconfs.BAM`          | `GetVersion(VersionRequest) returns (VersionResponse)`        |                                                              |
| `grpc.health.v1.Health` | `Check`                                                       | standard health service                                      |

#### Authentication

Send the token as gRPC metadata on every call, `GetVersion` included: key `x-token`, value the token. A token that is not entitled to at least one feed is refused with `UNAUTHENTICATED`.

#### Request

`SubscribeRequest` has two fields:

* `transactions`: a map from filter name to `TransactionFilter` (`account_include`, `account_required`, `signature`, `execution_results`; accounts and signatures as base58 strings).
* `region`: exactly one of `harmonic_region` or `bam_region`, matching the service called. Unspecified or the other feed's region is `INVALID_ARGUMENT`; a region the server does not serve is `FAILED_PRECONDITION`.

The limits in Filters apply; a request over them is `INVALID_ARGUMENT`.

#### Updates

`HarmonicUpdate` and `BamUpdate` carry `filters` (the names that matched, empty for slot boundaries and pings) and one payload: `transaction`, `ping`, `clip`, and on Harmonic `slot_start` and `slot_end`. The contract in The stream holds as described; pings arrive on quiet streams and can be ignored.

Transactions carry the raw bytes in `transaction`. The first signature is the 64 bytes after the compact-u16 signature count.

### Questions we get asked

**I filter on my accounts and see far fewer transactions than Geyser shows for the same accounts.** Two causes, in this order. Accounts a v0 transaction reaches through address lookup tables are not matched, only static keys are; see Address lookup tables. And only slots led by validators on the Harmonic or BAM client exist on the feeds; other leaders' slots have no preconfirmation anywhere. See What the feeds cover.

**Can I get the whole feed?** No. Every filter must select accounts or signatures, and the coverage share caps what one account receives. This is the condition under which the feeds are offered.

**Why was my stream closed with "cooling off" within a minute?** Wide filters, usually a whole program across many regions, took more than the share of the feed. Narrow them with `account_required`, drop the regions you cannot act on in time, and resubscribe after the cooloff. See Coverage.

**Is there an account\_exclude like other providers have?** Not yet. It is planned as an extra condition on a positive filter, never as a way to get everything except some accounts. Until then narrow with `account_required`. See account\_exclude.

**Should I subscribe to all regions?** No. A preconfirmation from a far region reaches you after the people located there have acted on it, so it is paid for and always late. Subscribe to the regions next to your servers, one stream each, and place servers where you need other regions. See Choosing regions.

**Which point of presence am I on?** `GetVersion` returns it. Anycast picks the closest; `Connector::dial` pins one.

**Am I billed for slots with no matching transactions?** On Harmonic yes: the slot fee is a floor for every slot your stream is open on. Pings and slot markers are not messages. See Billing.

**Does the `execution_results` filter cost anything?** No. Transactions it filters out are never delivered and never counted.

**Does a BAM subscription elsewhere give me BAM preconfs here?** No. Access is per Triton subscription; the feed is enabled on your subscription and used with your Triton token.

**Where exactly are the builders and nodes?** In the cities listed under Feeds and regions. The data centre is the feed operator's; Triton's points of presence sit close to them, and the region you subscribe is what decides the slots you get, not where you connect.


---

# Agent Instructions
This documentation is published with GitBook. GitBook is the documentation platform designed so that both humans and AI agents can read, navigate, and reason over technical content effectively. Learn more at gitbook.com.

## Querying This Documentation
If you need additional information that is not directly available in this page, you can query the documentation dynamically by asking a question.

Perform an HTTP GET request on the current page URL with the `ask` query parameter, and the optional `goal` query parameter:

```
GET https://docs.triton.one/chains/solana/preconfirmations-grpc.md?ask=<question>&goal=<endgoal>
```

`ask` is the immediate question: it should be specific, self-contained, and written in natural language.
`goal` is optional and describes the broader end goal you are ultimately trying to accomplish on behalf of the user. GitBook uses it to tailor the answer towards what is most useful for that goal.

The response will contain a direct answer to the question and relevant excerpts and sources from the documentation.

Use this mechanism when the answer is not explicitly present in the current page, you need clarification or additional context, or you want to retrieve related documentation sections.
