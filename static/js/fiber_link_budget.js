const LB_DEFAULTS = { tx: null, odc: "1:4", odp: "1:8", fiberKm: null, connectors: null, splices: null, compareOnt: null };
const LB_STATE = Object.assign({}, LB_DEFAULTS);
let lbTimer = null;
function lbEl(id) { return document.getElementById(id); }
function lbFmt1(n) { return Number(n).toLocaleString("id-ID", { minimumFractionDigits: 1, maximumFractionDigits: 1 }); }
function lbFmtInt(n) { return String(Math.round(Number(n))); }
function lbParseValue(raw, integer) {
  const t = String(raw == null ? "" : raw).trim();
  if (t === "") return null;
  const n = Number(t.replace(/\s/g, "").replace(",", "."));
  if (!Number.isFinite(n) || n < 0) return null;
  if (integer && !Number.isInteger(n)) return null;
  return n;
}
function lbSyncInputs() {
  lbEl("b_tx").value = LB_STATE.tx === null ? "" : lbFmt1(LB_STATE.tx);
  lbEl("b_km").value = LB_STATE.fiberKm === null ? "" : lbFmt1(LB_STATE.fiberKm);
  lbEl("b_conn").value = LB_STATE.connectors === null ? "" : lbFmtInt(LB_STATE.connectors);
  lbEl("b_spl").value = LB_STATE.splices === null ? "" : lbFmtInt(LB_STATE.splices);
  lbEl("b_sp1").value = LB_STATE.odc;
  lbEl("b_sp2").value = LB_STATE.odp;
  if (lbEl("b_ont")) lbEl("b_ont").value = LB_STATE.compareOnt || "";
}
function lbClearInvalid() {
  document.querySelectorAll(".lb-card .lb-input").forEach(function (el) { el.classList.remove("lb-invalid"); el.removeAttribute("aria-invalid"); });
}
function lbSchedule() {
  clearTimeout(lbTimer);
  lbTimer = setTimeout(calcBudget, 120);
}
function lbOnInput(id, key, integer) {
  const el = lbEl(id);
  const value = lbParseValue(el.value, integer);
  LB_STATE[key] = value;
  if (value === null) { el.classList.add("lb-invalid"); el.setAttribute("aria-invalid", "true"); }
  else { el.classList.remove("lb-invalid"); el.removeAttribute("aria-invalid"); }
  lbSchedule();
}
function lbOnBlur(id, decimals) {
  const el = lbEl(id);
  const key = el.id === "b_tx" ? "tx" : (el.id === "b_km" ? "fiberKm" : (el.id === "b_conn" ? "connectors" : "splices"));
  const value = LB_STATE[key];
  if (value === null || value === undefined) return;
  el.value = decimals ? lbFmt1(value) : lbFmtInt(value);
}
function lbOnSelectChange() {
  LB_STATE.odc = lbEl("b_sp1").value;
  LB_STATE.odp = lbEl("b_sp2").value;
  LB_STATE.compareOnt = lbEl("b_ont").value || null;
  lbSchedule();
}
function lbResetDefaults() {
  Object.assign(LB_STATE, LB_DEFAULTS);
  lbClearInvalid();
  lbSyncInputs();
  const err = lbEl("lbErr");
  if (err) err.textContent = "";
  calcBudget();
}
function lbInvalidState() {
  lbEl("lbRx").textContent = "\u2014";
  lbEl("lbBadge").hidden = true;
  lbEl("lbSummary").textContent = "Input belum valid. Lengkapi nilai Tx, serat, konektor, dan splice.";
  const tbody = document.querySelector("#lbTable tbody");
  if (tbody) tbody.innerHTML = "";
  lbRemoveComparison();
}
function lbRemoveComparison() {
  const vs = lbEl("lbVs"); if (vs) vs.remove();
  const tip = lbEl("lbTip"); if (tip) tip.remove();
}
function lbRenderComparison(j, rx) {
  lbRemoveComparison();
  if (j.actual_rx == null) return;
  const actual = Number(j.actual_rx);
  const gap = rx - actual;
  const dir = gap > 0 ? "lebih rendah " : "lebih tinggi ";
  const amount = lbFmt1(Math.abs(gap)) + " dB";
  const vs = document.createElement("div");
  vs.className = "lb-vs"; vs.id = "lbVs";
  vs.innerHTML = "Rx aktual <b>" + lbFmt1(actual) + " dBm</b> \u00b7 " + dir + amount + " dari teoritis.";
  lbEl("lbTable").before(vs);
  if (Math.abs(gap) > 2) {
    const tip = document.createElement("p");
    tip.className = "lb-tip"; tip.id = "lbTip";
    tip.textContent = "Kemungkinan konektor kotor, tikungan serat, atau splice buruk.";
    vs.after(tip);
  }
}
async function calcBudget() {
  const vals = [LB_STATE.tx, LB_STATE.fiberKm, LB_STATE.connectors, LB_STATE.splices];
  if (vals.some(function (v) { return v === null || v === undefined || !Number.isFinite(v); })) { lbInvalidState(); return; }
  const err = lbEl("lbErr"); if (err) err.textContent = "";
  const splitters = [];
  if (LB_STATE.odc) splitters.push(LB_STATE.odc);
  if (LB_STATE.odp) splitters.push(LB_STATE.odp);
  const body = { tx_dbm: LB_STATE.tx, splitters: splitters, fiber_km: LB_STATE.fiberKm, connectors: LB_STATE.connectors, splices: LB_STATE.splices };
  if (LB_STATE.compareOnt) body.ont_id = parseInt(LB_STATE.compareOnt, 10);
  try {
    const r = await fetch("/api/fiber/link-budget", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) { if (err) err.textContent = j.error || "Gagal menghitung."; return; }
    lbRenderResult(j);
  } catch (e) { if (err) err.textContent = e.message || "Gagal menghitung."; }
}
function lbRenderResult(j) {
  const L = j.losses || {};
  const rx = Number(j.expected_rx);
  const total = Number(L.total_db);
  const diff = rx - Number(TH.target);
  const st = rx < TH.crit ? "critical" : (rx < TH.warn ? "warning" : (rx <= TH.overload ? "normal" : "overload"));
  const names = { normal: "Normal", warning: "Peringatan", critical: "Kritis", overload: "Overload" };
  const badge = lbEl("lbBadge");
  badge.hidden = false; badge.textContent = names[st]; badge.className = "lb-badge lb-badge--" + st;
  lbEl("lbRx").textContent = lbFmt1(rx);
  const sign = diff >= 0 ? "+" : "\u2212";
  lbEl("lbSummary").innerHTML = "Total redaman <b>" + lbFmt1(total) + " dB</b> &middot; selisih dari target <b>" + sign + lbFmt1(Math.abs(diff)) + " dB</b>";
  const sl = { "1:2": 3.6, "1:4": 7.2, "1:8": 10.5, "1:16": 13.8, "1:32": 17.1, "1:64": 20.4 };
  const sp1 = sl[LB_STATE.odc] || 0, sp2 = sl[LB_STATE.odp] || 0;
  const fiberLoss = LB_STATE.fiberKm * 0.35, connLoss = LB_STATE.connectors * 0.5, spliceLoss = LB_STATE.splices * 0.1;
  let level = LB_STATE.tx;
  const rows = [{ name: "Tx OLT", desc: "", loss: null, level: level }];
  if (LB_STATE.odc) { level -= sp1; rows.push({ name: "Splitter ODC", desc: LB_STATE.odc, loss: sp1, level: level }); }
  if (LB_STATE.odp) { level -= sp2; rows.push({ name: "Splitter ODP", desc: LB_STATE.odp, loss: sp2, level: level }); }
  level -= fiberLoss; rows.push({ name: "Serat", desc: lbFmt1(LB_STATE.fiberKm) + " km", loss: fiberLoss, level: level });
  level -= connLoss; rows.push({ name: "Konektor", desc: LB_STATE.connectors + "x", loss: connLoss, level: level });
  level -= spliceLoss; rows.push({ name: "Splice", desc: LB_STATE.splices + "x", loss: spliceLoss, level: level });
  lbRenderRows(rows);
  lbRenderComparison(j, rx);
}
function lbRenderRows(rows) {
  const min = -35, max = 5;
  const width = function (v) { return Math.min(100, Math.max(0, (Number(v) - min) / (max - min) * 100)); };
  const color = function (v) { return v >= TH.overload ? "lb-fill--overload" : (v >= TH.warn ? "lb-fill--normal" : (v >= TH.crit ? "lb-fill--warning" : "lb-fill--critical")); };
  const tbody = document.querySelector("#lbTable tbody");
  tbody.innerHTML = rows.map(function (row) {
    const loss = row.loss === null ? "" : "\u2212" + lbFmt1(row.loss) + " dB";
    return "<tr><td class=\"lb-name\">" + row.name + "<span class=\"lb-desc\">" + row.desc + "</span></td>" +
      "<td class=\"lb-loss\">" + loss + "</td>" +
      "<td class=\"lb-bar-cell\"><div class=\"lb-track\"><div class=\"lb-fill " + color(row.level) + "\" style=\"width:" + width(row.level).toFixed(1) + "%\"></div></div></td>" +
      "<td class=\"lb-level\">" + lbFmt1(row.level) + "</td></tr>";
  }).join("");
}
setTimeout(function () { lbSyncInputs(); calcBudget(); }, 0);

