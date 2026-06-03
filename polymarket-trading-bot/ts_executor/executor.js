// executor.js — Ejecutor de órdenes Polymarket vía cliente TS oficial (v2)
// Uso:  node executor.js buy  <tokenId> <price> <size>
//       node executor.js sell <tokenId> <price> <size>
// Salida: una línea JSON  {"ok":true/false,"status":...,"orderID":...,"error":...}
//
// Parcheado para Exchange v2 (version "2" + direcciones nuevas) en los
// archivos dist del paquete. Si Polymarket termina de migrar la cuenta,
// las órdenes pasarán automáticamente.
const { ClobClient, Side, OrderType, SignatureType } = require("@polymarket/clob-client");
const { ethers } = require("ethers");
const fs = require("fs"); const path = require("path");

function loadEnv() {
  const env = {};
  for (const line of fs.readFileSync(path.join(__dirname, "..", ".env"), "utf8").split("\n")) {
    const m = line.match(/^([A-Z_0-9]+)=(.*)$/); if (m) env[m[1]] = m[2].trim();
  }
  return env;
}

function out(obj) { process.stdout.write(JSON.stringify(obj) + "\n"); }

(async () => {
  const [, , side, tokenId, priceStr, sizeStr] = process.argv;
  const price = parseFloat(priceStr), size = parseFloat(sizeStr);
  if (!["buy", "sell"].includes(side) || !tokenId || !price || !size) {
    out({ ok: false, error: "args: buy|sell <tokenId> <price> <size>" }); return;
  }
  const env = loadEnv();
  const PK = env.POLY_PRIVATE_KEY;
  const SAFE = "0xd3842227909efc0893047c88822cde2db750130e";
  const HOST = "https://clob.polymarket.com";

  try {
    const wallet = new ethers.Wallet(PK);
    let client = new ClobClient(HOST, 137, wallet, undefined, SignatureType.POLY_GNOSIS_SAFE, SAFE);
    const creds = await client.createOrDeriveApiKey();
    client = new ClobClient(HOST, 137, wallet, creds, SignatureType.POLY_GNOSIS_SAFE, SAFE);

    const negRisk = await client.getNegRisk(tokenId);
    let fee = 0;
    try { fee = await client.getFeeRateBps(tokenId); } catch (e) {}

    const order = await client.createOrder({
      tokenID: tokenId,
      price: price,
      side: side === "buy" ? Side.BUY : Side.SELL,
      size: size,
      feeRateBps: fee,
    }, { negRisk });

    const resp = await client.postOrder(order, OrderType.GTC);
    const orderID = resp && (resp.orderID || resp.orderId);
    const ok = !!(resp && (resp.success || orderID) && !resp.error);
    out({ ok, status: resp && resp.status, orderID: orderID || null, error: resp && resp.error || null, raw: resp });
  } catch (e) {
    out({ ok: false, error: (e && e.message) || String(e) });
  }
})();
