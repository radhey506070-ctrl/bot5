/* Supply Demand PRO | Valid Pattern + Retest  -  Pine indicator ka JS port.
   Website aur GitHub alert (node) dono yahi file use karte hain. */
(function (root) {
  const DEF = {
    bases: [1, 2, 3, 4, 5], baseMax: 50, openEdge: true, zoneEdge: true,
    legBodyOn: true, legIn: 40, legOut: 40, legLarger: true, legBasis: "Full Candle Range",
    legInMultOn: false, legInMult: 2, legOutMultOn: false, legOutMult: 2,
    noWick: true, volOn: true, volPct: 60,
    pats: { RBR: true, DBR: true, DBD: true, RBD: true },
    confBars: 3, confBody: 50, cancelEnter: true, curPrice: true, seq: false,
    retestOn: true, retestDir: false, touchTol: 0, slWick: true,
    slBufOn: true, slBuf: 0.5, rr: 2, oneTrade: true,
    maxZ: 50, disp: "Both", emaOn: true, emaLen: 20, emaRemove: true,
    look: 100, near: 1, pop: null
  };

  function levels(z, entry, S) {
    const buf = S.slBufOn ? entry * S.slBuf / 100 : 0, base = z.dem ? z.bot : z.top;
    const sl = z.dem ? base - buf : base + buf, risk = Math.abs(entry - sl);
    return { sl, tp: z.dem ? entry + risk * S.rr : entry - risk * S.rr };
  }

  function zoneScan(cd, S) {
    const O = cd.o, H = cd.h, L = cd.l, C = cd.c, V = cd.v || null, N = C.length; if (N < 9) return null;
    const body = i => Math.abs(C[i] - O[i]), rng = i => H[i] - L[i], pct = i => H[i] > L[i] ? body(i) / rng(i) * 100 : 0;
    const sz = S.legBasis == "Body" ? body : rng, bases = [...S.bases].sort((a, b) => a - b), tol = +S.touchTol || 0;
    // EMA (SMA se seed, jaise TradingView)
    const E = new Array(N).fill(NaN), len = Math.max(1, Math.round(S.emaLen)), k = 2 / (len + 1); let e = NaN, sum = 0;
    for (let i = 0; i < N; i++) { if (i < len - 1) sum += C[i]; else if (i == len - 1) { sum += C[i]; e = sum / len; E[i] = e; } else { e = C[i] * k + e * (1 - k); E[i] = e; } }

    function setup(t, n) {
      let bh = -1e18, bl = 1e18, bmc = -1e18, bnc = 1e18, btb = -1e18, bbb = 1e18, mb = 0;
      for (let i = t - n; i < t; i++) {
        if (pct(i) > S.baseMax) return null;
        bh = Math.max(bh, H[i]); bl = Math.min(bl, L[i]); bmc = Math.max(bmc, C[i]); bnc = Math.min(bnc, C[i]);
        btb = Math.max(btb, O[i], C[i]); bbb = Math.min(bbb, O[i], C[i]); mb = Math.max(mb, sz(i));
      }
      const li = t - n - 1, inS = sz(li), outS = sz(t);
      if (S.legLarger && !(inS > mb && outS > mb)) return null;
      if (S.legInMultOn && !(inS >= mb * S.legInMult)) return null;
      if (S.legOutMultOn && !(outS >= mb * S.legOutMult)) return null;
      if (S.legBodyOn && (pct(li) < S.legIn || pct(t) < S.legOut)) return null;
      const iu = C[li] > O[li], idn = C[li] < O[li], ou = C[t] > O[t], od = C[t] < O[t], bu = C[t] > bh, bd = C[t] < bl, P = S.pats;
      const rbr = iu && ou && bu && P.RBR, dbr = idn && ou && bu && P.DBR, dbd = idn && od && bd && P.DBD, rbd = iu && od && bd && P.RBD;
      if (!(rbr || dbr || dbd || rbd)) return null;
      const demand = rbr || dbr;
      if (S.noWick && !(demand ? L[t] >= btb : H[t] <= bbb)) return null;
      if (S.volOn && V && V[li] != null && V[t] != null && V[li] > 0 && !(V[t] >= V[li] * S.volPct / 100)) return null;
      const ph = Math.max(H[li], bh, H[t]), pl = Math.min(L[li], bl, L[t]); let top, bot;
      if (rbr) { top = S.openEdge ? btb : bmc; bot = Math.min(bl, L[t]); }
      else if (dbr) { top = bmc; bot = pl; }
      else if (dbd) { top = Math.max(bh, H[t]); bot = S.openEdge ? bbb : bnc; }
      else { top = ph; bot = bnc; }
      if (S.zoneEdge) { if (demand) top = bh; else bot = bl; }
      return { dem: demand, top, bot, lh: H[t], ll: L[t], bar: t };
    }

    let pend = [], zs = [], lDB = null, lST = null;
    for (let t = Math.max(6, N - 1 - S.look); t < N; t++) {
      // 1) pending setups: continuation confirmation
      for (let pi = pend.length - 1; pi >= 0; pi--) {
        const p = pend[pi], age = t - p.bar;
        const entered = L[t] <= p.top && H[t] >= p.bot, broke = p.dem ? C[t] < p.bot : C[t] > p.top;
        const early = S.cancelEnter && age >= 1 && (entered || broke);
        const conf = p.dem ? (C[t] > O[t] && C[t] > p.lh && pct(t) >= S.confBody) : (C[t] < O[t] && C[t] < p.ll && pct(t) >= S.confBody);
        if (age >= 1 && age <= S.confBars && conf && !early) {
          const okP = !S.curPrice || (p.dem ? p.top < C[t] : p.bot > C[t]);
          const okS = !S.seq || (p.dem ? (lDB === null || p.top < lDB) : (lST === null || p.bot > lST));
          if (okP && okS && p.top > p.bot) {
            zs.push({ top: p.top, bot: p.bot, dem: p.dem, c: t, tested: false, closed: false, res: 0, entry: NaN, sl: NaN, tp: NaN });
            if (p.dem) lDB = p.bot; else lST = p.top;
            if (zs.length > S.maxZ) zs.shift();
          }
          pend.splice(pi, 1);
        } else if (age > S.confBars || early) pend.splice(pi, 1);
      }
      // 2) naya setup (leg-out candle = current)
      let sel = null; for (const n of bases) { if (t - n - 1 >= 0) { const r = setup(t, n); if (r) sel = r; } }
      if (sel) pend.push(sel);
      // 3) zone management: retest, EMA, one-trade, SL / TP
      let open = 0; for (const z of zs) if (z.tested && !z.closed && !isNaN(z.sl)) open++;
      for (let zi = zs.length - 1; zi >= 0; zi--) {
        const z = zs[zi], after = t > z.c, sl = z.sl, tp = z.tp;
        const slB = z.dem ? (S.slWick ? L[t] <= sl : C[t] <= sl) : (S.slWick ? H[t] >= sl : C[t] >= sl);
        const tpB = z.dem ? (S.slWick ? H[t] >= tp : C[t] >= tp) : (S.slWick ? L[t] <= tp : C[t] <= tp);
        const closeBreak = z.dem ? C[t] < z.bot : C[t] > z.top;
        const fb = z.dem ? (S.slWick ? L[t] < z.bot : C[t] < z.bot) : (S.slWick ? H[t] > z.top : C[t] > z.top);
        const live = after && z.tested && !z.closed;
        const slHit = live && (isNaN(sl) ? fb : slB), tpHit = live && !isNaN(tp) && tpB && !slB;
        const touch = L[t] <= z.top + tol && H[t] >= z.bot - tol, dirOK = !S.retestDir || (z.dem ? C[t] > O[t] : C[t] < O[t]);
        const pe = z.dem ? Math.min(z.top, H[t]) : Math.max(z.bot, L[t]);
        const emaOK = !S.emaOn || (z.dem ? pe > E[t] : pe < E[t]);
        if (slHit || tpHit) { z.closed = true; z.res = slHit ? 2 : 3; }
        else if (after && !z.tested && S.emaRemove && touch && dirOK && !emaOK) zs.splice(zi, 1);
        else if (after && !z.tested && closeBreak) zs.splice(zi, 1);
        else if (after && !z.tested) {
          if (touch && dirOK && emaOK) {
            if (S.oneTrade && S.retestOn && open > 0) zs.splice(zi, 1);
            else {
              z.tested = true;
              if (S.retestOn) {
                const lv = levels(z, pe, S); z.entry = pe; z.sl = lv.sl; z.tp = lv.tp;
                const same = z.dem ? (S.slWick ? L[t] <= lv.sl : C[t] <= lv.sl) : (S.slWick ? H[t] >= lv.sl : C[t] >= lv.sl);
                if (same) { z.closed = true; z.res = 2; } else open++;
              }
            }
          }
        }
      }
    }
    // state: 0 fresh, 1 retested (trade live), 2 SL hit, 3 target hit
    const state = z => z.closed ? z.res : z.tested ? 1 : 0;
    const keep = z => S.disp == "Fresh Only" ? state(z) == 0 : S.disp == "Tested Only" ? state(z) >= 1 : state(z) <= 1;
    const out = z => { const st = state(z); let en = z.entry, s2 = z.sl, t2 = z.tp;
      if (isNaN(en)) { en = z.dem ? z.top : z.bot; const lv = levels(z, en, S); s2 = lv.sl; t2 = lv.tp; }
      return [z.top, z.bot, st, en, s2, t2]; };
    return { z: { demand: zs.filter(z => z.dem && keep(z)).map(out), supply: zs.filter(z => !z.dem && keep(z)).map(out) }, px: [C[N - 1], H[N - 1], L[N - 1]] };
  }

  /* criteria ke hisaab se stock ka sabse paas wala zone */
  function pick(v, zt, crit, S) {
    let best = null; const [c, h, l] = v.px, tol = +S.touchTol || 0;
    for (const [top, bot, state, entry, sl, tp] of v.z[zt] || []) {
      const touch = h >= bot - tol && l <= top + tol, dist = c > top ? (c - top) / c * 100 : c < bot ? (bot - c) / c * 100 : 0;
      const side = zt == "demand" ? c > top : c < bot;
      if ((crit == "zone" && touch) || (crit == "approach" && !touch && side && dist <= S.near))
        if (!best || dist < best.dist) best = { top, bot, state, entry, sl, tp, touch, dist, c };
    }
    return best;
  }
  const api = { DEF, zoneScan, pick };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.ZN = api;
})(this);
