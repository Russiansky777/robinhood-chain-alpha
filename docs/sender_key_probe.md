# Проба ключей отправителей

Ответы сервера дословно, значения ключей вычищены.

```
blockrazor (BLOCKRAZOR_AUTH_TOKEN): НИ ОДНА схема не принята. header:apikey -> http 500: {"signature":"","error":"rpc error: code = Unknown desc = illegal transaction"}
 | header:Authorization -> http 403: {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}
 | header:Authorization:Bearer -> http 403: {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}
 | header:x-api-key -> http 403: {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}
 | query:apikey -> http 403: {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}
 | query:api-key -> http 403: {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}
 | БЕЗ КЛЮЧА -> http 403: {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}

    header:apikey              http  500  непонятно: ответ не про ключ и не про тело  {"signature":"","error":"rpc error: code = Unknown desc = illegal transaction"}

    header:Authorization       http  403  ключ НЕ принят: сервер жалуется на ключ  {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}

    header:Authorization:Bearer http  403  ключ НЕ принят: сервер жалуется на ключ  {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}

    header:x-api-key           http  403  ключ НЕ принят: сервер жалуется на ключ  {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}

    query:apikey               http  403  ключ НЕ принят: сервер жалуется на ключ  {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}

    query:api-key              http  403  ключ НЕ принят: сервер жалуется на ключ  {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}

    БЕЗ КЛЮЧА                  http  403  ключ НЕ принят: сервер жалуется на ключ  {"signature":"","error":"error: Authentication information is missing. Please provide a valid auth token"}

astralane (ASTRALANE_API_KEY): ключ принят схемой header:apikey
    header:apikey              http  200  ключ принят: сервер жалуется на тело пробы, не на ключ  {"jsonrpc":"2.0","id":"проба","error":{"code":-32600,"message":"Invalid Request: invalid transaction \"failed to decode transaction only base64 is supported\""}
    header:Authorization       http  401  ключ НЕ принят: сервер жалуется на ключ  
    header:Authorization:Bearer http  401  ключ НЕ принят: сервер жалуется на ключ  
    header:x-api-key           http  200  ключ принят: сервер жалуется на тело пробы, не на ключ  {"jsonrpc":"2.0","id":"проба","error":{"code":-32600,"message":"Invalid Request: invalid transaction \"failed to decode transaction only base64 is supported\""}
    query:apikey               http  401  ключ НЕ принят: сервер жалуется на ключ  
    query:api-key              http  200  ключ принят: сервер жалуется на тело пробы, не на ключ  {"jsonrpc":"2.0","id":"проба","error":{"code":-32600,"message":"Invalid Request: invalid transaction \"failed to decode transaction only base64 is supported\""}
    БЕЗ КЛЮЧА                  http  401  ключ НЕ принят: сервер жалуется на ключ  
zeroslot (ZEROSLOT_API_KEY): секрета в окружении нет -- проба без ключа
    БЕЗ КЛЮЧА                  http  403  ключ НЕ принят: сервер жалуется на ключ  {"error":{"code":403,"message":"api-key does not exist"},"id":"1","jsonrpc":"2.0"}
```
