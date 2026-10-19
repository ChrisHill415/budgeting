const test = require('node:test');
const assert = require('node:assert/strict');
require('../public/finance.js');

const { occurrencesInRange } = globalThis.LedgerlyFinance;

test('one-time transaction is counted only on its scheduled date', () => {
  assert.deepEqual(occurrencesInRange({ date: '2026-10-22', cadence: 'once' }, '2026-10-01', '2026-10-21'), []);
  assert.deepEqual(occurrencesInRange({ date: '2026-10-22', cadence: 'once' }, '2026-10-01', '2026-10-22'), ['2026-10-22']);
});

test('custom daily income contributes once for each elapsed day', () => {
  assert.deepEqual(occurrencesInRange({ date: '2026-10-19', cadence: 'custom', custom_frequency: 'Every 1 day' }, '2026-10-01', '2026-10-21'), [
    '2026-10-19', '2026-10-20', '2026-10-21',
  ]);
});

test('custom intervals skip non-due dates', () => {
  assert.deepEqual(occurrencesInRange({ date: '2026-10-01', cadence: 'custom', custom_frequency: 'Every 2 weeks' }, '2026-10-01', '2026-10-31'), [
    '2026-10-01', '2026-10-15', '2026-10-29',
  ]);
});

test('monthly dates clamp to the last day of short months without drifting', () => {
  assert.deepEqual(occurrencesInRange({ date: '2026-01-31', cadence: 'monthly' }, '2026-02-01', '2026-04-30'), [
    '2026-02-28', '2026-03-31', '2026-04-30',
  ]);
});

test('daily schedule has no occurrences before its start date', () => {
  assert.deepEqual(occurrencesInRange({ date: '2026-10-23', cadence: 'custom', custom_frequency: 'Every 1 day' }, '2026-10-01', '2026-10-21'), []);
});
