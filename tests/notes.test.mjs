import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const here = path.dirname(fileURLToPath(import.meta.url));
eval(fs.readFileSync(path.join(here, '../src/notes.js'), 'utf8') + ';globalThis.parseNote=parseNote;');
{
const D=1440, MON10=600, TUE10=1440+600, hm=(h,m=0)=>h*60+m;
const cases = [
 ['Nouto arkisin ennen klo 14.', MON10, f=>f.pickup_close===hm(14)],
 ['Klar först på onsdag kl 12', MON10, f=>f.ready_at===2*D+hm(12)],
 ['Maksun jälkeen, ei ennen klo 13.30.', MON10, f=>f.release_at===hm(13,30)],
 ['Must be at destination by tomorrow 17:00.', MON10, f=>f.deadline_at===D+hm(17)],
 ['Vastaanotto klo 16 asti.', MON10, f=>f.delivery_close===hm(16)],
 ['Manuaalivaihteisto.', MON10, f=>f.manual],
 ['Car has no plates.', MON10, f=>f.plates],
 ['Auto vielä kesärenkailla.', MON10, f=>f.summer],
 ['Ready at two', MON10, f=>f.ready_at===hm(14)],
 ['Valmis puoli kolme', MON10, f=>f.ready_at===hm(14,30)],
 ['Klar halv tre', MON10, f=>f.ready_at===hm(14,30)],
 ['Ready tomorrow at 3pm, not 3pm sorry, 4pm', MON10, f=>f.ready_at===D+hm(16)],
 ['Valmis klo 12. Korjaus: klo 13.', MON10, f=>f.ready_at===hm(13)],
 ['Valmis to. klo 10', MON10, f=>f.ready_at===3*D+hm(10)],
 ['Ready Monday 9:00', TUE10, f=>f.ready_at===7*D+hm(9)],
 ['Klar i övermorgon kl 9', MON10, f=>f.ready_at===2*D+hm(9)],
 ['Kiitos! 2 autoa tulossa, 30 km matka.', MON10, f=>!f.ready_at&&!f.pickup_close&&!f.deadline_at],
 ['Nouto klo 8-16', MON10, f=>f.pickup_open===hm(8)&&f.pickup_close===hm(16)],
];
let ok=0; for (const [t,a,chk] of cases) { const f=parseNote(t,a); if (chk(f)) ok++; else console.log('FAIL', t, JSON.stringify(f)); }
console.log(ok+'/'+cases.length);

}
{
const D=1440, hm=(d,h,m=0)=>d*D+h*60+m, tod=(h,m=0)=>h*60+m;
const C = [
 ['valmis noudettavaksi huomenna klo 7 jälkeen. noudettavissa ma-pe 9-15:30. kiitos', hm(1,10,9), f=>f.ready_at===hm(2,7)&&f.pickup_close===tod(15,30)],
 ['Liike kiinni klo 16.30, haettava ennen sitä!', hm(1,10), f=>f.pickup_close===tod(16,30)&&f.delivery_close==null],
 ['plates removed, bring trade plates', 0, f=>f.plates],
 ['Huolto kesken, valmis to klo 13. Vastaanotto arkisin 7-20. Kiitos taas', hm(1,12,35), f=>f.ready_at===hm(3,13)&&f.delivery_close===tod(20)],
 ['Öppet 8 - 15.', 0, f=>f.pickup_close===tod(15)],
 ['huom aukioloajat klo 9.00 - 15.00', hm(1,12), f=>f.pickup_close===tod(15)&&f.ready_at==null],
 ['oltava perillä viimeistään ma klo 20', hm(1,15), f=>f.deadline_at===hm(7,20)],
 ['Asiakas tulee hakemaan, perillä viim. huomenna klo 15.', hm(1,17), f=>f.deadline_at===hm(2,15)],
 ['Luovutus vasta kun maksettu, aikaisintaan huomenna yhdeksältä', hm(2,15), f=>f.release_at===hm(3,9)],
 ['hei. voi noutaa huomenna klo 10 alkaen. noudettavissa arkisin 7-18. soita kun olet lähellä', hm(2,17), f=>f.ready_at===hm(3,10)],
 ['toimitus perille viim. klo 14.00! nouto ennen klo 18.00', hm(3,9), f=>f.deadline_at===hm(3,14)&&f.pickup_close===tod(18)],
 ['Auto valmis, nouto heti kun sopii. Liike kiinni klo 15.30, haettava ennen sitä. Lisäksi 4 kesärengasta tavaratilassa. Kiitti.', hm(4,11), f=>f.pickup_close===tod(15,30)&&!f.summer],
 ['Toimitus perille viim. klo 18. Kiitos.', hm(0,11), f=>f.deadline_at===hm(0,18)],
 ['haettavva ennen klo 16', hm(0,11), f=>f.pickup_close===tod(16)],
 ['Must be at destination by tomorrow 17:30. Correction: 16:30. Our ref: 880840', hm(0,19,44), f=>f.deadline_at===hm(1,16,30)],
 ['Saa hakea vasta huomenna klo 9 jälkeen. Toimitus perille viim. huomenna klo 19.', hm(0,20), f=>f.ready_at===hm(1,9)&&f.deadline_at===hm(1,19)],
 ['ostaja maksaa ensin, auton saa huomenna puoli kahdesta alkaen', hm(1,9), f=>f.release_at===hm(2,13,30)],
 ['Ostaja maksaa ensin, auton saa homenna klo 15 alkaen', hm(1,14), f=>f.release_at===hm(2,15)],
 ['Perillä trostaina klo 14 mennessä', hm(1,14), f=>f.deadline_at===hm(3,14)],
 ['Car has no plaates, bring trade plates.', 0, f=>f.plates],
 ['Noudettavissa arkisin 9 - 15', hm(2,18), f=>f.pickup_close===tod(15)&&f.ready_at==null],
 ['Odottaa osia, valmis huomenna puoli kolmelta. Liike sulkee 18.00.', hm(3,18), f=>f.ready_at===hm(4,14,30)&&f.pickup_close===tod(18)],
 ['Auki vain klo 14.30 asti. Vastaanottava liike auki arkipäivisin klo 7 - 20', hm(3,14), f=>f.pickup_close===tod(14,30)&&f.delivery_close===tod(20)],
 ['Kohde auki arkisin klo 7-20. Huom aukioloajat arkisin klo 8-17.', hm(4,15), f=>f.pickup_close===tod(17)],
 ['Auto vielä kesärenkailla.', 0, f=>f.summer],
];
let ok=0; for (const [t,a,c] of C) { const f=parseNote(t,a); if (c(f)) ok++; else console.log('FAIL', t, JSON.stringify({...f, trace: f.trace})); }
console.log(ok+'/'+C.length);

}
