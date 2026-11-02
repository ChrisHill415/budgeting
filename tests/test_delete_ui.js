const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
require('../public/finance.js');
const finance = globalThis.LedgerlyFinance;

function dashboard() {
  const elements = new Map();
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {value: selector === '#period' ? 'month' : '', innerHTML: '', textContent: '', style: {}, classList: {add() {}, remove() {}}, querySelector() {return null;}});
    return elements.get(selector);
  };
  const context = vm.createContext({
    document: {querySelector: element, querySelectorAll: () => []},
    window: {addEventListener() {}, confirm: () => true},
    LedgerlyFinance: finance, Intl, Date, setTimeout: () => 0, clearTimeout() {},
  });
  const source = fs.readFileSync(require.resolve('../public/app.js'), 'utf8');
  vm.runInContext(source.slice(0, source.indexOf("$('#auth-switch').addEventListener")), context);
  const start = source.indexOf("$('#all-activity-list').addEventListener('click',async e=>{");
  element('#all-activity-list').addEventListener = (_, handler) => {context.deleteHandler = handler;};
  vm.runInContext(source.slice(start, source.indexOf("$('#back-overview')", start)), context);
  return {context, element};
}

test('deletion updates balance, budget and chart even when overview reload fails', async () => {
  const {context, element} = dashboard();
  vm.runInContext(`
    const today=localDateISO(new Date());
    state.transactions=[
      {id:1,title:'Income',kind:'income',amount:1000,cadence:'once',date:today},
      {id:2,title:'Groceries',kind:'expense',amount:200,cadence:'once',date:today}
    ];
    render();
    api=async path=>{if(path==='/api/overview')throw Error('Connection lost');return {ok:true}};
  `, context);
  assert.match(element('#summary-grid').innerHTML, /\$800\.00/);
  assert.equal(element('#budget-spent').textContent, '$200.00');
  const originalChart = element('#cashflow-chart').innerHTML;
  const button = {disabled:false,dataset:{transactionId:'2'},classList:{contains:()=>false},closest:()=>({querySelectorAll:()=>[button]})};
  await context.deleteHandler({target:{closest:()=>button}});
  assert.match(element('#summary-grid').innerHTML, /\$1,000\.00/);
  assert.equal(element('#budget-spent').textContent, '$0.00');
  assert.notEqual(element('#cashflow-chart').innerHTML, originalChart);
  assert.doesNotMatch(element('#all-activity-list').innerHTML, /Groceries/);
  assert.equal(element('#toast').textContent, 'Connection lost');
});

test('overview response started before deletion cannot restore the deleted transaction', async () => {
  const {context} = dashboard();
  vm.runInContext(`
    let resolveOverview;
    api=()=>new Promise(resolve=>{resolveOverview=resolve});
    const pending=refresh();
    overviewRevision++;
    state.transactions=[];
    resolveOverview({transactions:[{id:2}],investments:[],monthly_budget:4000});
  `, context);
  await vm.runInContext('pending', context);
  assert.equal(vm.runInContext('state.transactions.length', context), 0);
});
