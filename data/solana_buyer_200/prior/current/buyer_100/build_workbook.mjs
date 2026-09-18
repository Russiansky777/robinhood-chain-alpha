import fs from 'node:fs/promises';
import {Workbook,SpreadsheetFile} from '@oai/artifact-tool';
const root='/workspace/scratch/47131fadf7ff/buyer_100';
const out='/workspace/scratch/47131fadf7ff/outputs/buyer100_20260917';
await fs.mkdir(out,{recursive:true});
const read=async n=>JSON.parse(await fs.readFile(`${root}/${n}.json`,'utf8'));
const rows=await read('selected'), points=await read('price_points'), fees=await read('fees'), analysis=await read('analysis');
const H=[5,15,30,60,180,300,900,3600,21600,86400], labels=['5 с','15 с','30 с','1 мин','3 мин','5 мин','15 мин','1 ч','6 ч','24 ч'];
const group=r=>r.zero_balance?'Нулевой остаток':'Докупка';
const pmap=new Map(points.map(p=>[`${p.signature}_${p.seconds}`,p]));
const fmap=new Map(fees.map(f=>[f.signature,f]));
const feeLabel={known_config_and_observed_fees:'Raydium CPMM: конфигурация и событие',observed_dynamic_fee:'Meteora DLMM: фактическая комиссия',observed_historical_input_fee:'Raydium CLMM: фактическая комиссия',config_matches_historical_fee:'Raydium CLMM: конфигурация подтверждена',observed_launch_fee:'Raydium LaunchLab: фактическая комиссия',unknown_pool_fee:'GatorSwap: комиссия не определена'};
const status={ok:'Есть цена',future:'Ещё рано',data_not_cached:'RPC: данные не загружены',route_decoder_missing:'GatorSwap: нет модели цены',launch_migration_requires_new_pool:'Нужен пул после миграции',quote_freshness_unverified:'Курс не проверен'};
const wb=Workbook.create();
const sum=wb.worksheets.add('Итоги'), buys=wb.worksheets.add('Покупки'), prices=wb.worksheets.add('Цены');
function base(s,range,title){s.showGridLines=false;s.getRange(range).format.font={name:'Arial',size:10,color:'#182B3A'};s.getRange(range).format.rowHeight=21;s.getRange(range).format.verticalAlignment='center';s.getRange(range).format.columnWidth=13;s.getRange('A2').values=[[title]];s.getRange('A2').format.font={name:'Arial',size:15,bold:true,color:'#182B3A'};}
function head(s,range){s.getRange(range).format={fill:'#263E50',font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},rowHeight:36,wrapText:true,horizontalAlignment:'center',verticalAlignment:'center'};}
function returns(s,range){s.getRange(range).setNumberFormat('0.00%;[Red](0.00%);0.00%');s.getRange(range).conditionalFormats.add('cellIs',{operator:'lessThan',formula:0,format:{font:{color:'#AE3535'}}});}
base(sum,'A1:L68','100 покупок больше 500 USDC');
sum.tabColor='#263E50';
sum.getRange('A3').values=[[`23 входа с нулевого остатка, 77 докупок. 39 токенов. Срез: ${analysis.meta.asof_utc.slice(0,16).replace('T',' ')} UTC.`]];
sum.getRange('A4').values=[['Ценовой расчёт НЕ ПОЛНЫЙ: 140 из 1 000 точек; 17 полных последовательностей до 5 минут.']];
sum.getRange('A4:L4').format.fill='#FFF1CC';
sum.getRange('A5').values=[['Средние ниже относятся только к доступным ценам. Это не результат всех 100 покупок.']];
sum.getRange('A7:L7').values=[['Группа','Горизонт','Выбрано','Есть цена','Нет цены','Ещё рано','Среднее','Медиана','Доля > 0','Доля ≥ 5%','Минимум','Максимум']];head(sum,'A7:L7');
base(buys,'A1:AB107','Все 100 подтверждённых покупок');
buys.getRange('A3').values=[['16.09.2026 03:23:13 — 17.09.2026 19:06:29 UTC. Порог: расход USDC строго больше 500.']];
buys.getRange('A4').values=[['Проценты = котировка / фактическая средняя цена покупки − 1. Расходы выхода и влияние объёма не вычтены.']];
buys.getRange('A5').values=[['Пустой процент означает отсутствие проверенной цены. Причины указаны справа и на листе «Цены».']];
buys.getRange('A7:AB7').values=[['№','Время UTC','Тип входа','Токен (mint)','Расход USDC','Получено токенов','Остаток до','Цена входа USDC','Комиссия выбранного пула',...labels,'Точек из 10','Причины пропусков','Слот','Индекс в блоке','Комиссия tx, SOL','Сигнатура','Источник','Площадка / комиссия','Пул котировки']];head(buys,'A7:AB7');
const bv=rows.map((r,i)=>{
 const f=fmap.get(r.signature), pp=H.map(s=>pmap.get(`${r.signature}_${s}`));
 const miss={};for(const p of pp)if(p.status!=='ok')miss[status[p.status]??p.status]=(miss[status[p.status]??p.status]??0)+1;
 const fee=f?.nominal_sum_rate??f?.observed_rate;
 return [i+1,new Date(r.time*1000),group(r),r.mint,Number(r.usdc_spent),Number(r.tokens_received),Number(r.balance_before),null,fee!=null?Number(fee):null,...H.map(()=>null),null,Object.entries(miss).map(([k,v])=>`${k}: ${v}`).join('; '),r.slot,r.transaction_index,Number(r.network_fee_sol),r.signature,`https://solscan.io/tx/${r.signature}`,feeLabel[f?.status]??f?.status??'Не определена',f?.pool??null];
});
buys.getRange('A8:AB107').values=bv;
buys.getRange('H8:H107').formulas=rows.map((_,i)=>[`=IF(AND(E${i+8}>0,F${i+8}>0),E${i+8}/F${i+8},NA())`]);
buys.getRange('J8:S107').formulas=rows.map((_,i)=>H.map((s,j)=>`='Цены'!G${8+i*10+j}`));
buys.getRange('T8:T107').formulas=rows.map((_,i)=>[`=COUNT(J${i+8}:S${i+8})`]);
buys.getRange('B8:B107').setNumberFormat('yyyy-mm-dd hh:mm:ss');
buys.getRange('E8:E107').setNumberFormat('#,##0.00');buys.getRange('F8:G107').setNumberFormat('#,##0.000000');buys.getRange('H8:H107').setNumberFormat('0.000000000000');
buys.getRange('I8:I107').setNumberFormat('0.00%');returns(buys,'J8:S107');buys.getRange('X8:X107').setNumberFormat('0.000000000');
buys.getRange('A1:A107').format.columnWidth=5;buys.getRange('B1:B107').format.columnWidth=23;buys.getRange('C1:C107').format.columnWidth=21;buys.getRange('D1:D107').format.columnWidth=51;
buys.getRange('F1:H107').format.columnWidth=21;buys.getRange('I1:I107').format.columnWidth=18;buys.getRange('J1:S107').format.columnWidth=12;buys.getRange('U1:U107').format.columnWidth=82;buys.getRange('Y1:Y107').format.columnWidth=102;buys.getRange('Z1:Z107').format.columnWidth=120;buys.getRange('AA1:AA107').format.columnWidth=40;
buys.getRange('AA1:AB107').format.columnWidth=56;
buys.freezePanes.freezeRows(7);buys.freezePanes.freezeColumns(3);buys.tables.add('A7:AB107',true,'Purchases');
base(prices,'A1:S1007','Цены и источники по каждому горизонту');
prices.getRange('A3').values=[['Время берётся из blockTime; секунды не переводятся в слоты через постоянный коэффициент.']];
prices.getRange('A4').values=[['Цена — предельная котировка пула в USDC. «К +5 с» — изменение этой котировки после задержанного входа без расходов.']];
prices.getRange('A5').values=[['Возраст цены и курса указан отдельно. Непроверенные курсы исключены из сводки.']];
prices.getRange('A7:S7').values=[['№ покупки','Секунд после','Группа','Целевое время UTC','Цена USDC','Цена входа USDC','К цене покупки','К цене +5 с','Статус','Слот цены','Время цены UTC','Возраст цены, с','Возраст курса, с','Пул котировки','Источник цены','Причина / источник','Обмены курса','Для медианы: нулевые','Для медианы: докупки']];head(prices,'A7:S7');
const pv=[];
rows.forEach((r,i)=>H.forEach((sec,j)=>{
 const p=pmap.get(`${r.signature}_${sec}`), leg=p.legs?.[0], quotes=p.legs?.slice(1)??[];
 pv.push([i+1,sec,null,new Date(p.target*1000),p.price_usdc?Number(p.price_usdc):null,null,null,null,status[p.status]??p.status,leg?.slot??null,leg?.time?new Date(leg.time*1000):null,leg?.age_seconds??null,quotes.length?Math.max(...quotes.map(q=>q.age_seconds??0)):null,leg?.pool??null,leg?.signature?`https://solscan.io/tx/${leg.signature}`:null,p.missing??p.source??p.status,quotes.map(q=>`${q.pool}; slot ${q.slot}; age ${q.age_seconds}s`).join(' | '),null,null]);
}));
prices.getRange('A8:S1007').values=pv;
prices.getRange('C8:C1007').formulas=pv.map((_,i)=>[`='Покупки'!C${8+Math.floor(i/10)}`]);
prices.getRange('F8:F1007').formulas=pv.map((_,i)=>[`='Покупки'!H${8+Math.floor(i/10)}`]);
prices.getRange('G8:H1007').formulas=pv.map((_,i)=>{const r=8+i,b=8+Math.floor(i/10)*10;return [`=IF(I${r}="Есть цена",IF(AND(ISNUMBER(E${r}),E${r}>0,F${r}>0),E${r}/F${r}-1,NA()),"")`,`=IF(AND(I${r}="Есть цена",I${b}="Есть цена"),IF(AND(E${r}>0,E${b}>0),E${r}/E${b}-1,NA()),"")`];});
prices.getRange('R8:S1007').formulas=pv.map((_,i)=>{const r=8+i;return [`=IF(AND(C${r}="Нулевой остаток",I${r}="Есть цена"),G${r},"")`,`=IF(AND(C${r}="Докупка",I${r}="Есть цена"),G${r},"")`];});
prices.getRange('D8:D1007').setNumberFormat('yyyy-mm-dd hh:mm:ss');prices.getRange('K8:K1007').setNumberFormat('yyyy-mm-dd hh:mm:ss');prices.getRange('E8:F1007').setNumberFormat('0.000000000000');returns(prices,'G8:H1007');returns(prices,'R8:S1007');
prices.getRange('A1:B1007').format.columnWidth=11;prices.getRange('C1:C1007').format.columnWidth=21;prices.getRange('D1:F1007').format.columnWidth=23;prices.getRange('I1:I1007').format.columnWidth=33;prices.getRange('K1:K1007').format.columnWidth=23;prices.getRange('N1:N1007').format.columnWidth=53;prices.getRange('O1:Q1007').format.columnWidth=115;prices.getRange('R1:S1007').format.columnWidth=21;
prices.getRange('I8:I1007').conditionalFormats.add('notContainsText',{text:'Есть цена',format:{fill:'#FFF1CC',font:{color:'#725A1E'}}});
prices.freezePanes.freezeRows(7);prices.freezePanes.freezeColumns(2);prices.tables.add('A7:S1007',true,'PricePoints');
const groups=[['Все','all'],['Нулевой остаток','zero'],['Докупка','add']];
let sr=8;
for(const [name,key] of groups)for(let j=0;j<H.length;j++,sr++){
 const crit=key==='all'?'':`, 'Цены'!$C$8:$C$1007,"${name}"`;
 const ok=`COUNTIFS('Цены'!$B$8:$B$1007,${H[j]},'Цены'!$I$8:$I$1007,"Есть цена"${crit})`;
 const fut=`COUNTIFS('Цены'!$B$8:$B$1007,${H[j]},'Цены'!$I$8:$I$1007,"Ещё рано"${crit})`;
 const mc=key==='all'?'G':key==='zero'?'R':'S';const refs=rows.map((_,i)=>`'Цены'!${mc}${8+i*10+j}`).join(',');
 sum.getRange(`A${sr}:B${sr}`).values=[[name,labels[j]]];
 sum.getRange(`C${sr}:L${sr}`).formulas=[[
 key==='all'?"=COUNT('Покупки'!$A$8:$A$107)":`=COUNTIFS('Покупки'!$C$8:$C$107,"${name}")`,
 `=${ok}`,`=C${sr}-D${sr}-F${sr}`,`=${fut}`,
 `=IF(D${sr}=0,"",AVERAGE(${refs}))`,
 `=IF(D${sr}=0,"",MEDIAN(${refs}))`,
 `=IF(D${sr}=0,"",COUNTIFS('Цены'!$B$8:$B$1007,${H[j]},'Цены'!$I$8:$I$1007,"Есть цена",'Цены'!$G$8:$G$1007,">0"${crit})/D${sr})`,
 `=IF(D${sr}=0,"",COUNTIFS('Цены'!$B$8:$B$1007,${H[j]},'Цены'!$I$8:$I$1007,"Есть цена",'Цены'!$G$8:$G$1007,">=0.05"${crit})/D${sr})`,
 `=IF(D${sr}=0,"",MIN(${refs}))`,`=IF(D${sr}=0,"",MAX(${refs}))`
 ]];
}
returns(sum,'G8:L37');sum.getRange('A1:A68').format.columnWidth=22;sum.getRange('B1:B68').format.columnWidth=11;sum.getRange('C1:F68').format.columnWidth=11;
sum.getRange('A40').values=[['Сопоставимые 17 покупок: полные точки от +5 секунд до +5 минут']];sum.getRange('A40').format.font.bold=true;
sum.getRange('A42:H42').values=[['Группа','Горизонт','Покупок','Среднее к входу','Медиана к входу','Среднее к +5 с','Медиана к +5 с','Доля > 0 к +5 с']];head(sum,'A42:H42');
sum.getRange('A43:H60').values=analysis.cohort.map(x=>[groups.find(g=>g[1]===x.group)[0],labels[H.indexOf(x.seconds)],x.leader.n,x.leader.mean,x.leader.median,x.after_5s.mean,x.after_5s.median,x.after_5s.positive_share]);returns(sum,'D43:H60');
sum.getRange('A62').values=[['Сравнение 17 случаев — фиксированный срез, не независимая проверка оптимального выхода и не выборка всех 100.']];
sum.getRange('A63').values=[['Нулевой остаток не означает первую покупку за всю историю. Учтены балансы участвовавших токен-счетов.']];
sum.getRange('A64').values=[['39 из 100 покупок относятся к одному mint 6GmAFSYs…UNgx; все они — докупки.']];
sum.getRange('A65').values=[['USDC≈USD — допущение. Входная цена включает фактический расход / чистое получение токенов.']];
sum.getRange('A66').values=[['Комиссия tx может быть оплачена другим адресом. Котировка и комиссия относятся к выбранному пулу маршрута.']];
sum.getRange('A67').values=[['Загрузка остановлена ошибкой сетевого доступа CONNECT 403. Пропуски не заменены нулевой доходностью.']];
sum.getRange('A68').values=[['Источники: сохранённые RPC-транзакции и блоки Solana; ссылки на покупки и ценовые обмены — в детализации.']];
const originalPrice=prices.getRange('E8').values[0][0];
prices.getRange('E8').values=[[originalPrice*1.01]];
const changed=prices.getRange('G8').values[0][0];
if(typeof changed!=='number'||Math.abs(changed-(originalPrice*1.01/Number(rows[0].entry_usdc)-1))>1e-9)throw new Error('Price change did not propagate');
prices.getRange('E8').values=[[null]];
if(typeof prices.getRange('G8').values[0][0]==='number')throw new Error('Missing price became numeric return');
prices.getRange('E8').values=[[originalPrice]];
wb.recalculate();
const vals=sum.getRange('C8:L37').values;
for(let i=0;i<30;i++){
 const a=analysis.stats[i];
 if(vals[i][1]!==a.n)throw new Error(`Count mismatch at row ${i+8}: ${vals[i][1]} vs ${a.n}`);
 for(const [col,k] of [[4,'mean'],[5,'median'],[6,'positive_share'],[7,'at_least_5_share'],[8,'minimum'],[9,'maximum']]){
  if(a.n&&(typeof vals[i][col]!=='number'||!Number.isFinite(vals[i][col])||Math.abs(vals[i][col]-a[k])>1e-9))throw new Error(`${k} mismatch ${i}: ${vals[i][col]} vs ${a[k]}`);
 }
}
const err=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:20},summary:'Formula error scan'});
await fs.writeFile(`${out}/formula_checks.json`,err.ndjson??JSON.stringify(err));
const views=process.argv.includes('--summary-only')?[['Итоги','A2:L18','summary']]:[['Итоги','A2:L18','summary'],['Покупки','A7:M13','purchases'],['Цены','A7:M13','prices']];
for(const [sheet,range,name] of views){
 const png=await wb.render({sheetName:sheet,range,scale:1.4,format:'png'});await fs.writeFile(`${out}/${name}.png`,new Uint8Array(await png.arrayBuffer()));
}
const xlsx=await SpreadsheetFile.exportXlsx(wb);await xlsx.save(`${out}/buyer_100_purchases.xlsx`);
console.log(JSON.stringify({file:`${out}/buyer_100_purchases.xlsx`,purchases:rows.length,points:points.length,matched_summary_rows:30}));
