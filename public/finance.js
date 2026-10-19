// Calendar based transaction scheduling shared by the dashboard and charts.
(function (root) {
  const DAY_MS = 24 * 60 * 60 * 1000;

  function parts(iso) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
    if (!match) return null;
    const value = { year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) };
    const date = new Date(Date.UTC(value.year, value.month - 1, value.day));
    return date.getUTCFullYear() === value.year && date.getUTCMonth() === value.month - 1 && date.getUTCDate() === value.day ? value : null;
  }

  function iso(value) {
    return `${value.year}-${String(value.month).padStart(2, '0')}-${String(value.day).padStart(2, '0')}`;
  }

  function fromUtc(time) {
    const date = new Date(time);
    return { year: date.getUTCFullYear(), month: date.getUTCMonth() + 1, day: date.getUTCDate() };
  }

  function cadence(transaction) {
    if (transaction.cadence === 'once') return null;
    if (transaction.cadence === 'daily') return { unit: 'day', interval: 1 };
    if (transaction.cadence === 'weekly') return { unit: 'week', interval: 1 };
    if (transaction.cadence === 'monthly') return { unit: 'month', interval: 1 };
    if (transaction.cadence === 'yearly') return { unit: 'year', interval: 1 };
    if (transaction.cadence === 'custom') {
      const match = /^Every\s+(\d+)\s+(days?|weeks?|months?|years?)$/i.exec(String(transaction.custom_frequency || '').trim());
      if (match && Number(match[1]) > 0) return { unit: match[2].toLowerCase().replace(/s$/, ''), interval: Number(match[1]) };
    }
    return null;
  }

  function occurrenceAt(start, schedule, index) {
    const { unit, interval } = schedule;
    if (unit === 'day' || unit === 'week') {
      const step = interval * (unit === 'week' ? 7 : 1);
      return fromUtc(Date.UTC(start.year, start.month - 1, start.day) + index * step * DAY_MS);
    }
    const monthStep = interval * (unit === 'year' ? 12 : 1);
    const monthIndex = start.year * 12 + start.month - 1 + index * monthStep;
    const year = Math.floor(monthIndex / 12);
    const month = monthIndex % 12 + 1;
    const lastDay = new Date(Date.UTC(year, month, 0)).getUTCDate();
    return { year, month, day: Math.min(start.day, lastDay) };
  }

  function occurrencesInRange(transaction, fromISO, throughISO) {
    const start = parts(transaction.date), from = parts(fromISO), through = parts(throughISO);
    if (!start || !from || !through || iso(from) > iso(through)) return [];
    const schedule = cadence(transaction);
    if (!schedule) return transaction.cadence === 'once' && iso(start) >= iso(from) && iso(start) <= iso(through) ? [iso(start)] : [];

    let index = 0;
    if (schedule.unit === 'day' || schedule.unit === 'week') {
      const stepDays = schedule.interval * (schedule.unit === 'week' ? 7 : 1);
      const dayDiff = Math.floor((Date.UTC(from.year, from.month - 1, from.day) - Date.UTC(start.year, start.month - 1, start.day)) / DAY_MS);
      index = Math.max(0, Math.ceil(dayDiff / stepDays));
    } else {
      const monthDiff = (from.year - start.year) * 12 + from.month - start.month;
      const stepMonths = schedule.interval * (schedule.unit === 'year' ? 12 : 1);
      index = Math.max(0, Math.ceil(monthDiff / stepMonths));
    }
    const result = [];
    for (let date = occurrenceAt(start, schedule, index); iso(date) <= iso(through); date = occurrenceAt(start, schedule, ++index)) {
      if (iso(date) >= iso(from)) result.push(iso(date));
    }
    return result;
  }

  root.LedgerlyFinance = { occurrencesInRange };
})(typeof window === 'undefined' ? globalThis : window);
