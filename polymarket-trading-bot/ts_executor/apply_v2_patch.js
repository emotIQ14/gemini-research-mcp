// apply_v2_patch.js — NO-OP (mantener cliente oficial canónico)
//
// HALLAZGO FINAL (verificado 2026-06): la CLOB API de Polymarket rechaza las
// órdenes de TODAS las librerías públicas actuales con:
//   {"error":"invalid order version, please use the latest clob-client"}
// incluso con @polymarket/clob-client 5.8.1 y order-utils 3.0.1 (las últimas
// publicadas) y con firma EIP-712 válida. El servidor exige un formato de orden
// más nuevo que el que existe públicamente. Parchear a "v2" NO ayuda (lo probamos).
//
// ACCIÓN CORRECTA cuando Polymarket publique el cliente nuevo:
//   cd ts_executor && npm update @polymarket/clob-client
// La cola de reintento del bridge ejecutará las órdenes automáticamente en cuanto
// el cliente actualizado produzca un formato aceptado.
//
// Este script ya NO modifica nada (el cliente canónico es el correcto).
console.log("[no-op] Cliente oficial canónico. Cuando Polymarket publique el clob-client v2,");
console.log("        ejecuta: npm update @polymarket/clob-client  (sin parches manuales).");
