import fs from "node:fs";
import { routeEdges } from "@mr_mint/elkjs-libavoid";

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const routes = await routeEdges(input.graph, input.options ?? {});

if (input.edgeId) {
  const route = routes.get(input.edgeId);
  if (!route) {
    throw new Error(`libavoid returned no route for ${input.edgeId}`);
  }
  process.stdout.write(JSON.stringify(route));
} else {
  process.stdout.write(JSON.stringify(Object.fromEntries(routes)));
}
