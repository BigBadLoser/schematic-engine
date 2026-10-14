// usage: node run_elk.mjs <bundle> <graphs.json> <out.json>; lays out each graph in the array.
import fs from 'fs'; import { createRequire } from 'module';
const ELK = createRequire(import.meta.url)(process.argv[2]);
const graphs = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const elk = new ELK(); const out = [];
for (const g of graphs) { try { out.push(await elk.layout(g)); } catch (e) { out.push({ error: String(e) }); } }
fs.writeFileSync(process.argv[4], JSON.stringify(out));
