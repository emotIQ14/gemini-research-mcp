// apply_v2_patch.js — Re-aplica el fix Exchange v2 al cliente TS instalado.
// Ejecutar tras `npm install` (node_modules no se versiona).
//   node apply_v2_patch.js
//
// Polymarket migró a Exchange v2 (junio 2026):
//   - CTF Exchange estándar:  0xE111180000d2663C0091e4f400237545B87B996B
//   - Neg Risk CTF Exchange:  0xe2222d279d744050d28e00520010520000310F59
//   - EIP-712 domain version: "2"  (el paquete trae "1")
// El paquete @polymarket/clob-client 5.8.1 aún trae direcciones viejas + v1.
const fs = require("fs");
const path = require("path");

const constFile = path.join(__dirname, "node_modules/@polymarket/clob-client/dist/order-utils/exchange.order.const.js");
const cfgFile   = path.join(__dirname, "node_modules/@polymarket/clob-client/dist/config.js");

let changed = 0;

// 1. PROTOCOL_VERSION "1" -> "2"
let c = fs.readFileSync(constFile, "utf8");
if (c.includes('PROTOCOL_VERSION = "1"')) {
  c = c.replace('export const PROTOCOL_VERSION = "1";', 'export const PROTOCOL_VERSION = "2";');
  fs.writeFileSync(constFile, c); changed++;
  console.log("[patch] PROTOCOL_VERSION -> 2");
} else if (c.includes('PROTOCOL_VERSION = "2"')) {
  console.log("[ok] PROTOCOL_VERSION ya es 2");
}

// 2. Direcciones v2 en config.js
let cfg = fs.readFileSync(cfgFile, "utf8");
const before = cfg;
cfg = cfg.replace(/"0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"/g, '"0xE111180000d2663C0091e4f400237545B87B996B"');
cfg = cfg.replace(/negRiskExchange: "0xC5d563A36AE78145C45a50134d48A1215220f80a"/g, 'negRiskExchange: "0xe2222d279d744050d28e00520010520000310F59"');
if (cfg !== before) { fs.writeFileSync(cfgFile, cfg); changed++; console.log("[patch] direcciones Exchange v2 aplicadas"); }
else console.log("[ok] direcciones ya parcheadas");

console.log(changed ? `\n✅ Patch v2 aplicado (${changed} cambios)` : "\n✅ Ya estaba parcheado");
