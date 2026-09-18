from pathlib import Path
import json,datetime,zipfile,collections
p=Path(__file__).parent
rows=json.loads((p/'selected.json').read_text());audit=json.loads((p/'selection_audit.json').read_text());prices=json.loads((p/'price_points.json').read_text()) if (p/'price_points.json').exists() else []
config={'wallet':'Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit','target_count':100,'threshold_exclusive':500,'observed_quote_currency':'USDC','usd_parity_is_assumption':True,'groups':['all','zero_balance_before','positive_balance_before'],'horizons_seconds':[5,15,30,60,180,300,900,3600,21600,86400],'reference_price':'actual_quote_spend/net_tokens_received','secondary_comparison':'price_at_horizon/price_at_5_seconds - 1','network_actions':'read_only','status':'in_progress','last_checked_signature':audit[-1]['signature'],'checked_successful_transactions':len(audit),'selected_so_far':len(rows),'zero_balance':sum(r['zero_balance'] for r in rows),'price_status_counts':dict(collections.Counter(x['status'] for x in prices)),'updated_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
(p/'checkpoint.json').write_text(json.dumps(config,indent=2))
s=f'''# Покупки Beqv6dz…Xqeit свыше 500 USDC

Промежуточный результат. Работа продолжается; полный анализ 100 покупок пока не готов.

Проверено транзакций: **{len(audit)}**. Отобрано покупок: **{len(rows)}/100**, из них с нулевым остатком — **{config['zero_balance']}**, докупок — **{len(rows)-config['zero_balance']}**.

Ценовые точки: {json.dumps(config['price_status_counts'],ensure_ascii=False)}. Это число точек, а не полностью рассчитанных покупок. Пропуски и будущие горизонты не равны нулевой доходности.

Порог строгий: расход USDC >500; USDC≈USD — допущение. Покупки выбраны последовательно от последней сохранённой истории назад. По каждой проверяется подпись владельца и итоговые изменения токенов. Нулевой остаток определяется по участвовавшим токен-счетам владельца перед операцией; это не обязательно первая покупка за всю историю адреса. Необычные схемы с несколькими токен-счетами требуют дополнительной проверки.

Цена входа = USDC / чистое число полученных токенов. Последующие цены — предельные цены пулов, пересчитанные в USDC по историческим обменам; это не исполнимая цена продажи полного объёма. Изменения к средней цене лидера не означают доходность копировщика, вошедшего после него. Сетевые комиссии, tips, влияние объёма и расходы на последующую продажу не вычтены.

Горизонты по фактическому blockTime: 5, 15, 30, 60, 180, 300, 900, 3600, 21600, 86400 секунд. Используется последнее доступное состояние на конец блока с временем не позже точки. Возраст котировки и источники пересчёта записываются отдельно. Для неоднозначных операций и неподдерживаемых моделей остаются явные статусы.

| UTC | Mint | USDC | Токенов чистыми | Цена входа, USDC | Остаток до | Группа | Слот | Транзакция |
|---|---|---:|---:|---:|---:|---|---:|---|
'''
for r in rows:
 t=datetime.datetime.fromtimestamp(r['time'],datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S');group='Нулевой остаток' if r['zero_balance'] else 'Докупка'
 s+=f"| {t} | {r['mint']} | {r['usdc_spent']} | {r['tokens_received']} | {float(r['entry_usdc']):.12g} | {r['balance_before']} | {group} | {r['slot']} | [{r['signature'][:10]}…](https://solscan.io/tx/{r['signature']}) |\n"
s+='''
Код и данные: select.py — отбор; selection_audit.json — каждая проверенная операция; selected.json — покупки; engine.py — декодирование; routes.py — цепочки конвертации; price_batch.py — расчёт горизонтов; price_points.json — результаты и источники; observations — исходные состояния; tx и block_* — RPC-ответы; checkpoint.json — состояние работы. Полные новые блоки сохраняются отдельно в buyer_100_blocks_*.zip; их состав — raw_manifest.json. В архиве исходные файлы старого анализа не дублируются; engine.py использует decode.py и DLMM IDL из solana_three_check, включённые в архив.
'''
(p/'buyer_100_status.md').write_text(s)
with zipfile.ZipFile(p.parent/'buyer_100_checkpoint.zip','w',zipfile.ZIP_DEFLATED,compresslevel=3) as z:
 for f in p.rglob('*'):
  if f.is_file() and '__pycache__' not in str(f) and f.suffix not in ['.tmp'] and not (f.name.startswith('block_') and f.name[6:].split('.')[0].isdigit()):
   z.write(f,f.relative_to(p.parent),compress_type=zipfile.ZIP_STORED if f.suffix=='.gz' else zipfile.ZIP_DEFLATED)
 for rel in ['decode.py','pool_meta.json','schemas/dlmm.json','schemas/states_config.rs']:
  f=p.parent/'solana_three_check'/rel;z.write(f,f.relative_to(p.parent))
print(json.dumps(config,ensure_ascii=False))
