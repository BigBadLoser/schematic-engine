// Routes many frozen libavoid payloads in one Node process (the replay lab's worker).
//
// stdin : JSON array of {key, graph, options}  (the same graph/options the engine sends to
//         scripts/libavoid_route.mjs, one entry per recorded call and variant)
// stdout: JSON object {key: {ok: true, routes: {edgeId: RouteResult}} | {ok: false, error}}
//
// The CLI copies this file to <work>/lab/ so that Node resolves <work>/node_modules, the same
// package the engine uses.
import fs from "node:fs";
import { routeEdges } from "@mr_mint/elkjs-libavoid";

const jobs = JSON.parse(fs.readFileSync(0, "utf8"));
const results = {};
for (const job of jobs) {
  try {
    const routes = await routeEdges(job.graph, job.options ?? {});
    results[job.key] = { ok: true, routes: Object.fromEntries(routes) };
  } catch (error) {
    results[job.key] = { ok: false, error: String((error && error.message) || error) };
  }
}
process.stdout.write(JSON.stringify(results));
