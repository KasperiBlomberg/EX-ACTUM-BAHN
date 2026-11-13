// Dealer-note reader (fi/sv/en), JS port of spreadcore/notes.py. Follows RULES.md:
// days count from arrival; weekday names mean 2..6 days ahead; digits are 24h unless am/pm;
// hours in words are working hours 9..17 ("two" = 14:00, "half three" = 14:30); corrections win;
// denied times ("not 3pm") are ignored.
const N_DAY = 1440;
const WEEKDAYS_RX = [
  ['maanantai\\w*|måndag\\w*|monday|mån|mon', 0], ['tiistai\\w*|tisdag\\w*|tuesday|tues|tue|tis', 1],
  ['keskiviik\\w*|onsdag\\w*|wednesday|wed|ons', 2], ['torstai\\w*|trostai\\w*|torsdag\\w*|thursday|thurs|thur|thu|tors', 3],
  ['perjantai\\w*|fredag\\w*|friday|fri|fre', 4], ['lauantai\\w*|lördag\\w*|saturday|sat|lör', 5],
  ['sunnuntai\\w*|söndag\\w*|sunday|sun|sön', 6],
];
const FI_SHORT = { ma: 0, ti: 1, ke: 2, to: 3, pe: 4, la: 5, su: 6 };
const REL_DAYS = [
  ['day after tomorrow|ylihuomen\\w*|i\\s?övermorgon|overmorrow', 2],
  ['huo?m+en+a\\p{L}*|hom+en+a\\p{L}*|huomena|i\\s?morgon|tomorrow|tomorow|tommorow|tmrw|tmrrw|tmr|2moro', 1],
  ['tänään|idag|i dag|today|tdy', 0],
];
const NUM_WORDS = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12,
  yksi: 1, yhdeltä: 1, yhteen: 1, kaksi: 2, kahdelta: 2, kahteen: 2, kolme: 3, kolmelta: 3, kolmeen: 3, neljä: 4, neljältä: 4, neljään: 4,
  viisi: 5, viideltä: 5, viiteen: 5, yhdeksän: 9, yhdeksältä: 9, kymmenen: 10, kymmeneltä: 10, kymmeneen: 10, yksitoista: 11,
  yhdeltätoista: 11, kaksitoista: 12, kahdeltatoista: 12, yhdestä: 1, kahdesta: 2, kolmesta: 3, neljästä: 4, viidestä: 5, kuusi: 6, kuudelta: 6, kuuteen: 6, kuudesta: 6, seitsemän: 7, seitsemältä: 7, seitsemään: 7, seitsemästä: 7, kahdeksan: 8, kahdeksalta: 8, kahdeksaan: 8, kahdeksasta: 8, yhdeksään: 9, yhdeksästä: 9, kymmenestä: 10, yhdestätoista: 11, kahdestatoista: 12, sju: 7, åtta: 8, ett: 1, två: 2, tre: 3, fyra: 4, fem: 5, nio: 9, tio: 10, elva: 11, tolv: 12 };
const WB = '(?<![\\p{L}\\d])', WE = '(?![\\p{L}\\d])';     // unicode-aware word boundaries
const TIME_CUES = 'klo|kl\\.?|klockan|at|@|by|before|after|until|till|til|from|ennen|jälkeen|asti|saakka|mennessä|alkaen|lähtien|viimeistään|aikaisintaan|före|efter|innan|från|senast|tidigast|not|ei|inte|ej|but|vaan|utan|men|or|tai|eller|around|noin|ca\\.?|cirka|ready|valmis|klar';
const DAY_CUES = 'huo?m+en+a|tänään|ylihuomenna|tomorrow|today|idag|imorgon|morgon|maanantaina|tiistaina|keskiviikkona|torstaina|perjantaina|på måndag|på tisdag|på onsdag|på torsdag|på fredag|monday|tuesday|wednesday|thursday|friday|vasta|först|alkaen';
const CUE_BEFORE = new RegExp(`${WB}(?:${TIME_CUES}|${DAY_CUES})\\s*$`, 'u');
const UNITS = /^\s*(?:km|%|€|eur|e\b|autoa|auto|cars?|bilar|bil|kpl|st\b|min|h\b|tuntia|timmar|hours?|pv|days?|päivä)/u;
const NEG = new RegExp(`${WB}(?:not|ei|inte|ej|no)\\s+(?:klo\\s+|kl\\.?\\s+|klockan\\s+|at\\s+|on\\s+|på\\s+)?$`, 'u');
const K = {
  payment: 'maksu\\p{L}*|maksa\\p{L}*|maksettu|payment|paid|pay(?![\\p{L}])|betalning|betald|betalat|invoice|lasku',
  ready: 'valmi\\p{L}*|ready|klar(?![\\p{L}])|färdig|available|saatavilla|noudettavissa|vapautuu|frigörs',
  pickup: 'nouto\\p{L}*|nouda\\p{L}*|noudet\\p{L}*|haku|haett?av\\p{L}*|hakea|pick\\s?-?up|pick\\s+up|collect\\p{L}*|hämt\\p{L}*|avhämt\\p{L}*|luovutus\\p{L}*|överlämn\\p{L}*|handover|hand-over',
  site: 'vastaanot\\p{L}*|receiv\\p{L}*|reception|deliveries\\s+(?:until|till)|mottag\\p{L}*|open(?![\\p{L}])|auki|öppe\\p{L}*|close\\p{L}*|closing|suljet\\p{L}*|sulke\\p{L}*|sulkeu\\p{L}*|stäng\\p{L}*|kiinni',
  deadline: 'perill\\p{L}*|perille|destination|framme|deliver(?:ed|y)?\\s+by|must\\s+be|oltava|pitää\\s+olla|täytyy|needed|tarvitaan|behövs|deadline|latest|viimeistään|senast|mennessä|arrive\\p{L}*|saapu\\p{L}*|toimit\\p{L}*|lever\\p{L}*',
};
const DEST = /kohde|vastaanot|receiv|mottag|destination|perill|perille|leverans|framme|määränpää/u;
const KRX = Object.fromEntries(Object.entries(K).map(([k, v]) => [k, new RegExp(v, 'gu')]));
const REL_BEFORE = /before|until|till|by|ennen|asti|saakka|mennessä|viimeistään|före|innan|senast|latest/gu;
const REL_AFTER = /after|from|jälkeen|alkaen|lähtien|aikaisintaan|earliest|efter|från|tidigast|not\s+before|ei\s+ennen|inte\s+före|ej\s+före|först/gu;
const F_MANUAL = /manuaali\p{L}*|käsivaiht\p{L}*|manual(?![\p{L}])|manuell\p{L}*|stick\s?shift|kopplingspedal/u;
const F_AUTO_NEG = /(?:not|ei|inte|ej)\s+(?:a\s+)?(?:manuaali|manual|manuell)/u;
const F_PLATES = /no\s+(?:licen[cs]e\s+)?pla+tes|trade\s+pla+tes|pla+tes\s+removed|no\s+reg(?:istration)?\s+pla+tes|without\s+plates|plates?\s+missing|needs?\s+(?:transfer\s+)?plates|unregistered|ei\s+kilpi\p{L}*|ilman\s+kilpi\p{L}*|kilvet\p{L}*|kilpiä\s+ei|kilvet\s+puuttu\p{L}*|rekisteröimät\p{L}*|siirtokilp\p{L}*|koekilp\p{L}*|koenumer\p{L}*|inga\s+skyltar|utan\s+skyltar|saknar\s+skyltar|avställd|provskylt\p{L}*|transfer\s+plates/u;
const F_SUMMER = /kesärenkailla|kesärenkaat\s+alla|kesärenkaat\s+vielä|vielä\s+kesärenkaat|(?:on|still\s+(?:has|on|with))\s+summer\s+t[iy]res|summer\s+t[iy]res\s+(?:on|still|fitted)|på\s+sommardäck|har\s+sommardäck|sommardäck\s+på|fortfarande\s+sommardäck/u;
const DIGIT_RE = /(?<![\d.:])(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?:\s*-\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?)?(?!\d)/gu;
const WORD_RE = new RegExp(`${WB}(?:(half|puoli|halv)\\s+)?(${Object.keys(NUM_WORDS).sort((a, b) => b.length - a.length).join('|')})${WE}`, 'gu');

const wordHour = n => (n >= 9 && n <= 12) ? n : n + 12;
const ampm = (h, a) => { if (!a) return h; a = a.replace(/\./g, ''); if (a === 'am') return h === 12 ? 0 : h; return h === 12 ? h : h + 12; };
function lastPos(rx, s) { let pos = -1; rx.lastIndex = 0; for (const m of s.matchAll(rx)) pos = m.index; return pos; }

function findTimes(s) {
  const out = [];
  for (const m of s.matchAll(DIGIT_RE)) {
    const [_, h0, mi, ap, h2, mi2, ap2] = m; const h = +h0; const prefix = s.slice(0, m.index);
    const cue = CUE_BEFORE.test(prefix.slice(-25));
    if (UNITS.test(s.slice(m.index + m[0].length)) && !mi && !ap) continue;
    if (!(mi || ap || cue || h2)) continue;
    if (h > 23 || (mi && +mi > 59)) continue;
    const tod = ampm(h, ap || ap2) * 60 + (mi ? +mi : 0);
    let tod2 = null; if (h2 != null) { if (+h2 > 23) continue; tod2 = ampm(+h2, ap2) * 60 + (mi2 ? +mi2 : 0); }
    out.push({ start: m.index, end: m.index + m[0].length, tod, tod2, denied: NEG.test(prefix) });
  }
  for (const m of s.matchAll(WORD_RE)) {
    const [_, half, word] = m; const prefix = s.slice(0, m.index);
    if (!half && !CUE_BEFORE.test(prefix.slice(-25))) continue;
    if (UNITS.test(s.slice(m.index + m[0].length))) continue;
    const n = NUM_WORDS[word];
    const tod = half ? (wordHour(n) - 1) * 60 + 30 : wordHour(n) * 60;
    out.push({ start: m.index, end: m.index + m[0].length, tod, tod2: null, denied: NEG.test(prefix) });
  }
  return out.sort((a, b) => a.start - b.start);
}
function findDays(s, arrivalDay) {
  const found = [], taken = [];
  const free = i => !taken.some(([a, b]) => a <= i && i < b);
  const wAbs = w => { let d = ((w - arrivalDay % 7) % 7 + 7) % 7; if (d < 2) d += 7; return arrivalDay + d; };
  for (const [rx, delta] of REL_DAYS) for (const m of s.matchAll(new RegExp(`${WB}(?:${rx})`, 'gu'))) { if (!free(m.index)) continue; taken.push([m.index, m.index + m[0].length]); found.push([m.index, arrivalDay + delta, NEG.test(s.slice(0, m.index))]); }
  for (const [rx, w] of WEEKDAYS_RX) for (const m of s.matchAll(new RegExp(`${WB}(?:${rx})${WE}\\.?`, 'gu'))) { if (!free(m.index)) continue; taken.push([m.index, m.index + m[0].length]); found.push([m.index, wAbs(w), NEG.test(s.slice(0, m.index))]); }
  for (const m of s.matchAll(/(?<![\p{L}\d])(ma|ti|ke|to|pe|la|su)\.?(?=\s*(?:klo|kl\b|$|,)|\.\s*\d)/gu)) { if (!free(m.index)) continue; found.push([m.index, wAbs(FI_SHORT[m[1]]), NEG.test(s.slice(0, m.index))]); }
  return found.sort((a, b) => a[0] - b[0]);
}
function sentences(t) {
  t = t.replace(/(\d)\.(\d)/g, '$1§$2').replace(/(?<![\p{L}\d])(ma|ti|ke|to|pe|la|su)\./gu, '$1¤');
  return t.split(/[.!?;\n]+/).map(p => p.replace(/§/g, '.').replace(/¤/g, '.').trim()).filter(Boolean);
}
function classify(sentence, tm) {
  const pre = sentence.slice(0, tm.start), post = sentence.slice(tm.end, tm.end + 20), window = pre + ' ' + post;
  let obj = null, best = -1;
  for (const [name, rx] of Object.entries(KRX)) {
    let p = lastPos(rx, pre);
    if (p < 0) p = lastPos(rx, post) < 0 ? -1 : 0;
    if (p > best) { best = p; obj = name; }
  }
  KRX.payment.lastIndex = 0; if (KRX.payment.test(sentence)) obj = 'payment';
  const pb = lastPos(REL_BEFORE, window), pa = lastPos(REL_AFTER, window);
  const rel = (pa >= 0 && pa >= pb) ? 'after' : pb >= 0 ? 'before' : null;
  return [obj, rel];
}
function parseNote(text, arrivalMinute) {
  const f = { ready_at: null, release_at: null, deadline_at: null, pickup_open: null, pickup_close: null, delivery_close: null, manual: false, plates: false, summer: false, trace: [] };
  if (!text) return f;
  const t = text.toLowerCase().replace(/[–—]/g, '-').replace(/’/g, "'").replace(/\s+/g, ' ')
    .replace(/(?<![\p{L}])viim\./gu, 'viimeistään').replace(/(?<![\p{L}])huom\./gu, 'huom').replace(/(?<![\p{L}])n\.(?=\s*\d)/gu, 'noin').replace(/(?<![\p{L}])esim\./gu, 'esim').replace(/klo\./g, 'klo');
  f.manual = F_MANUAL.test(t) && !F_AUTO_NEG.test(t); f.plates = F_PLATES.test(t); f.summer = F_SUMMER.test(t);
  const aDay = Math.floor(arrivalMinute / N_DAY);
  let last = null;
  for (const s of sentences(t)) {
    const days = findDays(s, aDay).filter(d => !d[2]);
    const times = findTimes(s);
    for (const tm of times) {
      if (tm.denied) continue;
      const before = days.filter(d => d[0] <= tm.start), after = days.filter(d => d[0] > tm.start);
      const explicitDay = before.length ? before[before.length - 1][1] : after.length ? after[0][1] : null;
      const [obj, rel] = classify(s, tm);
      const dest = DEST.test(s);
      let fld;
      if (tm.tod2 != null) fld = dest ? 'delivery_close' : 'pickup_close';              // a range is opening hours
      else if (obj === 'site') fld = dest ? 'delivery_close' : 'pickup_close';          // "liike kiinni klo 16.30"
      else fld = obj === 'payment' ? 'release_at' : obj === 'ready' ? 'ready_at' : obj === 'pickup' ? (rel === 'after' ? 'ready_at' : 'pickup_close')
        : obj === 'deadline' ? 'deadline_at' : rel === 'after' ? 'ready_at' : last;
      if (!fld) continue;
      const inherited = explicitDay == null && fld === last && f[fld] != null && fld.endsWith('_at') ? Math.floor(f[fld] / N_DAY) : null;
      const day = explicitDay ?? inherited ?? aDay;
      const abs = day * N_DAY + tm.tod;
      if (fld === 'pickup_close' || fld === 'delivery_close') {
        if (tm.tod2 != null) { if (fld === 'pickup_close') f.pickup_open = tm.tod; f[fld] = tm.tod2; } else f[fld] = tm.tod;
      } else f[fld] = abs;
      f.trace.push(`${fld}<-${s}`); last = fld;
    }
    if (!times.length && days.length) {
      const d = days[days.length - 1][1];
      KRX.payment.lastIndex = 0; KRX.ready.lastIndex = 0; KRX.deadline.lastIndex = 0;
      if (KRX.ready.test(s) || KRX.payment.test(s)) { f.ready_at = d * N_DAY + 8 * 60; last = 'ready_at'; }
      else if (KRX.deadline.test(s)) { f.deadline_at = d * N_DAY + 20 * 60; last = 'deadline_at'; }
    }
  }
  return f;
}
