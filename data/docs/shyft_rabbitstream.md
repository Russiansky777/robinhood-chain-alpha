<!-- источник: https://docs.shyft.to/solana-shredstreaming/how-to-stream-with-rabbitstream.md -->
<!-- забрано: 2026-09-24T15:13:33Z, код ответа: 200 -->

> For the complete documentation index, see [llms.txt](https://docs.shyft.to/llms.txt). Markdown versions of documentation pages are available by appending `.md` to page URLs; this page is available as [Markdown](https://docs.shyft.to/solana-shredstreaming/how-to-stream-with-rabbitstream.md).

# How to stream with RabbitStream

Rabbitstream Quickstart: Connecting with TypeScript and Rust

RabbitStream is designed to be a seamless, <mark style="color:yellow;">drop-in replacement</mark> for your existing Yellowstone gRPC clients, requiring only a change of endpoint URL.

{% hint style="info" %}

#### Beta-access already available for everyone with a <mark style="color:yellow;">Shyft gRPC</mark> token.

{% endhint %}

### Access and Authentication

RabbitStream requires a valid <mark style="color:yellow;">Shyft gRPC</mark> <mark style="color:yellow;">token</mark>. gRPC access is included with our *Build, Grow*, and *Accelerate* plans. Once you have your token, you can access the stream using the following regional endpoints:

| **Ams** (Amsterdam)        | `https://rabbitstream.ams.shyft.to/` |
| -------------------------- | ------------------------------------ |
| **VA** (Virginia, Ashburn) | `https://rabbitstream.va.shyft.to/`  |
| NY (New York)              | `https://rabbitstream.ny.shyft.to/`  |
| **Fra** (Frankfurt)        | `https://rabbitstream.fra.shyft.to/` |

### Connecting to RabbitStream: Client Examples

Once you have your Shyft gRPC Access Token, you can connect to Rabbitstream in the following manner:

{% tabs %}
{% tab title="TypeScript" %}

```typescript
import "dotenv/config";

import Client, {
  CommitmentLevel,
  SubscribeRequest,
  SubscribeRequestAccountsDataSlice,
} from "@triton-one/yellowstone-grpc";

const client = new Client(
  "https://rabbitstream.ams.shyft.to/",
  process.env.X_TOKEN,
  undefined
);

const req: SubscribeRequest = {
  accounts: {},
  slots: {},
  transactions: {
    pumpFun: {
      vote: false,
      failed: false,
      signature: undefined,
      accountInclude: ["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"],
      accountExclude: [],
      accountRequired: [],
    },
  },
  transactionsStatus: {},
  entry: {},
  blocks: {},
  blocksMeta: {},
  accountsDataSlice: [] as SubscribeRequestAccountsDataSlice[],
  ping: undefined,
  commitment: CommitmentLevel.PROCESSED,
};

async function handleStream(client: Client, args: SubscribeRequest) {
  // Subscribe for events
  console.log(`Subscribing and starting stream...`);
  const stream = await client.subscribe();

  // Create `error` / `end` handler
  const streamClosed = new Promise<void>((resolve, reject) => {
    stream.on("error", (error) => {
      console.log("ERROR", error);
      reject(error);
      stream.end();
    });
    stream.on("end", () => {
      resolve();
    });
    stream.on("close", () => {
      resolve();
    });
  });

  // Handle updates
  stream.on("data", (data) => {
    console.log("Received data....");
    console.dir(data, { depth: null });
  });

  // Send subscribe request
  await new Promise<void>((resolve, reject) => {
    stream.write(args, (err: any) => {
      if (err === null || err === undefined) {
        resolve();
      } else {
        reject(err);
      }
    });
  }).catch((reason) => {
    console.error(reason);
    throw reason;
  });

  await streamClosed;
}

async function subscribeCommand(client: Client, args: SubscribeRequest) {
  while (true) {
    try {
      await handleStream(client, args);
    } catch (error) {
      console.error("Stream error, restarting in 1 second...", error);
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }
}

subscribeCommand(client, req);

```

{% endtab %}

{% tab title="Rust" %}

```rust
use {
    backoff::{future::retry, ExponentialBackoff},
    clap::Parser as ClapParser,
    futures::{
        future::TryFutureExt,         
        stream::StreamExt,
    },
    log::{error, info},
    std::{collections::HashMap, env, sync::Arc, time::Duration},
    tokio::sync::Mutex,
    tonic::transport::channel::ClientTlsConfig,
    yellowstone_grpc_client::{GeyserGrpcClient, Interceptor},
    yellowstone_grpc_proto::{
        geyser::SubscribeRequestFilterTransactions,
        prelude::{ CommitmentLevel, SubscribeRequest},
    },
};


type TxnFilterMap = HashMap<String, SubscribeRequestFilterTransactions>;

const PUMPFUN_AMM_PROGRAM_ID: &str = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA";


#[derive(Debug, Clone, ClapParser)]
#[clap(author, version, about)]
struct Args {
    #[clap(short, long, help = "gRPC endpoint")]
    endpoint: String,

    #[clap(long, help = "X-Token")]
    x_token: String,
}

impl Args {
    async fn connect(&self) -> anyhow::Result<GeyserGrpcClient<impl Interceptor>> {
        GeyserGrpcClient::build_from_shared(self.endpoint.clone())?
            .x_token(Some(self.x_token.clone()))?
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(10))
            .tls_config(ClientTlsConfig::new().with_native_roots())?
            .max_decoding_message_size(1024 * 1024 * 1024)
            .connect()
            .await
            .map_err(Into::into)
    }

    pub fn get_txn_updates(&self) -> anyhow::Result<SubscribeRequest> {
        let mut transactions: TxnFilterMap = HashMap::new();

        transactions.insert(
            "client".to_owned(),
            SubscribeRequestFilterTransactions {
                vote: Some(false),
                failed: Some(false),
                account_include: vec![PUMPFUN_AMM_PROGRAM_ID.to_string()],
                account_exclude: vec![],
                account_required: vec![],
                signature: None,
            },
        );

        Ok(SubscribeRequest {
            accounts: HashMap::default(),
            slots: HashMap::default(),
            transactions,
            transactions_status: HashMap::default(),
            blocks: HashMap::default(),
            blocks_meta: HashMap::default(),
            entry: HashMap::default(),
            commitment: Some(CommitmentLevel::Processed as i32),
            accounts_data_slice: Vec::default(),
            ping: None,
            from_slot: None,
        })
    }
}



#[tokio::main]
async fn main() -> anyhow::Result<()> {
    env::set_var(
        env_logger::DEFAULT_FILTER_ENV,
        env::var_os(env_logger::DEFAULT_FILTER_ENV).unwrap_or_else(|| "info".into()),
    );
    env_logger::init();

    let args = Args::parse();
    let zero_attempts = Arc::new(Mutex::new(true));

    retry(ExponentialBackoff::default(), move || {
        let args = args.clone();
        let zero_attempts = Arc::clone(&zero_attempts);

        async move {
            let mut zero_attempts = zero_attempts.lock().await;
            if *zero_attempts {
                *zero_attempts = false;
            } else {
                info!("Retry to connect to the server");
            }
            drop(zero_attempts);

            let client = args.connect().await.map_err(backoff::Error::transient)?;
            info!("Connected");

            let request = args.get_txn_updates().map_err(backoff::Error::Permanent)?;

            geyser_subscribe(client, request)
                .await
                .map_err(backoff::Error::transient)?;

            Ok::<(), backoff::Error<anyhow::Error>>(())
        }
        .inspect_err(|error| error!("failed to connect: {error}"))
    })
    .await
    .map_err(Into::into)
}

async fn geyser_subscribe(
    mut client: GeyserGrpcClient<impl Interceptor>,
    request: SubscribeRequest,
) -> anyhow::Result<()> {
    let ( subscribe_tx, mut stream) = client.subscribe_with_request(Some(request)).await?;
    info!("stream opened");


    while let Some(message) = stream.next().await {
        match message {
            Ok(msg) => println!("Received Message: {:#?}", msg),
            Err(e) => error!("Failed to receive message: {e}"),
        }
    }

    info!("stream closed");
    Ok(())
}
```

{% endtab %}
{% endtabs %}

### Performance Benchmarks: The Speed Advantage

RabbitStream is ideal for applications that need <mark style="color:yellow;">low-latency transaction monitoring</mark>, such as *DeFi dashboards, arbitrage bots, or token analytics tools*. We compared the detection latency between RabbitStream and Yellowstone gRPC. All the code for this comparison tool is available on GitHub for you to try out.

<div data-full-width="false"><figure><img src="https://2394289113-files.gitbook.io/~/files/v0/b/gitbook-x-prod.appspot.com/o/spaces%2F4XnSsBH76iytbI7NwX5o%2Fuploads%2F9h6lT7xICF59GNQ6hHDn%2Ftoken-2.png?alt=media&amp;token=ff47aef6-5046-47ed-a485-d8530ea58bd5" alt=""><figcaption></figcaption></figure></div>
