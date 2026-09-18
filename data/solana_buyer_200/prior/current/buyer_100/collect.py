from rpc import *
W='Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit';A='Fbck4smimeuWmxAUU4PKwJGYYVoxzXFhrwDgmTk8eQH3'
h=rpc('getSignaturesForAddress',[A,{'limit':1000}],'usdc_history_0');print('USDC history',len(h),'newest',h[0]['blockTime'],'oldest',h[-1]['blockTime'],flush=True)
