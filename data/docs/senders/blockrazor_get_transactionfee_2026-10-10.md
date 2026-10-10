---
description: Introducing the access method of BlockRazor Solana Get TransactionFee
metaLinks:
  canonical: get-transactionfee.md
  alternates:
    - >-
      https://app.gitbook.com/s/QJcHRn7SY50Ny5UQhXHy/streams/network-fee-stream/solana/get-transactionfee
---

# Solana Get TransactionFee

### Introduction

`Get TransactionFee` is used to obtain the priority fee and tip of Solana transactions in aggregate, supports gRPC protocol, endpoint: <mark style="color:$primary;">grpc.solana-fee.blockrazor.me:443</mark>

### Limit

| User Type | Limit  | Price        | Action                                                                                                                                                                                                         |
| --------- | ------ | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| paid user | 10 QPS | $300 / month | <a href="https://blockrazor.io/#/login?redirect=pricing&#x26;purchaseMode=personalized&#x26;chain=solana&#x26;serviceId=solana_network_fee_stream&#x26;billing=day" class="button primary small">Subscribe</a> |

### Request Parameter

<table><thead><tr><th width="111.8515625">Parameters</th><th width="129.8203125">Mandatory</th><th width="99.4765625">Format</th><th width="121.24609375">Exampl</th><th>Remark</th></tr></thead><tbody><tr><td>accounts</td><td>optional</td><td>string[]</td><td>["DH4xma……HFtNYJ"]</td><td>If account is not specified, priority fee and tip of all transactions in the specified slot range will be counted.</td></tr><tr><td>percentile</td><td>mandatory</td><td>int</td><td>50</td><td>Get the priority fee and tip of the specified quantile, enumerated value: 25, 50, 75, 95, 99</td></tr><tr><td>slotRange</td><td>mandatory</td><td>int</td><td>150</td><td>Statistics of priorityFee and tip of transactions in the last N confirmed slots, N ranges from 1 to 150</td></tr></tbody></table>

### Request Example

```go
package main

import (
	"context"
	"crypto/tls"
	"log"

	// directory of the generated code using the provided proto file
	pb "fee-test/feepb"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

const (
	gRPCEndpoint = "grpc.solana-fee.blockrazor.xyz:443" // endpoint address
	auth         = "your auth" // auth to be verified
	testAccount1 = "DH4xmaWDnTzKXehVaPSNy9tMKJxnYL5Mo5U3oTHFtNYJ" // query priority fee and tip of transactions involving a specified account 
	testAccount2 = "CAPhoEse9xEH95XmdnJjYrZdNCA8xfUWdy3aWymHa1Vj" 
)

func main() {
	// open gRPC connection to endpoint
	conn, err := grpc.Dial(
		gRPCEndpoint,
		grpc.WithTransportCredentials(credentials.NewTLS(&tls.Config{})),
		grpc.WithPerRPCCredentials(&Authentication{auth}),
	)
	if err != nil {
		panic(err)
	}
	defer conn.Close()

	// use the gRPC client
	client := pb.NewServerClient(conn)

	req := &pb.TransactionFee{
		Accounts:   []string{}, //do not set accounts
		Percentile: 75,
		SlotRange:  150,
	}
  
  // send gRPC request1
	resp, err := client.GetTransactionFee(context.Background(), req)
	if err != nil {
		panic(err)
	}
	log.Printf("Response PriorityFee Percentile: %+v", resp.PriorityFee.Percentile)
	log.Printf("Response PriorityFee Value: %+v", resp.PriorityFee.Value)
	log.Printf("Response Tip Percentile: %+v", resp.Tip.Percentile)
	log.Printf("Response Tip Value: %+v", resp.Tip.Value)
	
	req2 := &pb.TransactionFee{
		Accounts:   []string{testAccount1, testAccount2}, //set specified accounts
		Percentile: 75,
		SlotRange:  150,
	}
	
	// send gRPC request2
	resp2, err := client.GetTransactionFee(context.Background(), req2)
	if err != nil {
		panic(err)
	}
	log.Printf("Response2 PriorityFee Percentile: %+v", resp2.PriorityFee.Percentile)
	log.Printf("Response2 PriorityFee Value: %+v", resp2.PriorityFee.Value)
	log.Printf("Response2 Tip Percentile: %+v", resp2.Tip.Percentile)
	log.Printf("Response2 Tip Value: %+v", resp2.Tip.Value)
}

type Authentication struct {
	auth string
}

func (a *Authentication) GetRequestMetadata(context.Context, ...string) (map[string]string, error) {
	return map[string]string{"apiKey": a.auth}, nil
}

func (a *Authentication) RequireTransportSecurity() bool {
	return false
}

```

#### Proto

```json
syntax = "proto3";

package feepb;

option go_package = "./pb/feepb";

service Server {
    rpc GetTransactionFee(TransactionFee) returns(TransactionFeeResponse) {};
}

message TransactionFee {
    repeated string accounts = 1;
    int32 percentile = 2;
    int32 slotRange = 3;
}

message TransactionFeeResponse {
    FeeValue priorityFee = 1;
    FeeValue tip = 2;
}

message FeeValue {
    int32 percentile = 1;
    double value = 2;
}
```

### Response Example

```json
Response PriorityFee Percentile: 75
Response PriorityFee Value: 10000 // The priority fee corresponding to the quantile, in micro-lamports
Response Tip Percentile: 75 
Response Tip Value: 0.0001 // The tip corresponding to the quantile, in Sol
Response2 PriorityFee Percentile: 75
Response2 PriorityFee Value: 432313.2435833086
Response2 Tip Percentile: 75
Response2 Tip Value: 0.0001
```





