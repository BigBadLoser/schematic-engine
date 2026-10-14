// Which libavoid features does the pinned libavoid-js binding expose? (review finding C1)
//
// Runs from <work>/probes/ so that Node resolves <work>/node_modules. The CLI
// copies this file there and runs it after `bootstrap`. Prints one JSON object.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const modules = path.resolve(here, "..", "node_modules");
const result = { name: "libavoid_surface", ok: false, kicad_mcp_version: null, summary: [], data: {} };

const finish = (code) => {
  process.stdout.write(JSON.stringify(result) + "\n");
  process.exit(code);
};
const names = (object) => (object ? Object.getOwnPropertyNames(object).filter((n) => n !== "constructor").sort() : []);

try {
  const version = JSON.parse(fs.readFileSync(path.join(modules, "libavoid-js", "package.json"), "utf8")).version;
  const mod = await import("libavoid-js");
  const AvoidLib = mod.AvoidLib ?? mod.default?.AvoidLib ?? mod.default;
  try {
    await AvoidLib.load();
  } catch (error) {
    await AvoidLib.load(path.join(modules, "libavoid-js", "dist", "libavoid.wasm"));
  }
  const Avoid = AvoidLib.getInstance();
  const exported = Object.keys(Avoid).sort();
  const hyperedgeExports = exported.filter((name) => /hyperedge/i.test(name));

  const classes = {};
  for (const name of ["Router", "ConnRef", "ConnEnd", "JunctionRef", "ShapeRef", "ShapeConnectionPin"]) {
    classes[name] = Avoid[name] ? names(Avoid[name].prototype) : null;
  }

  let routerHyperedgeRerouter = "not attempted";
  try {
    const router = new Avoid.Router(Avoid.RouterFlag.OrthogonalRouting.value);
    routerHyperedgeRerouter = typeof router.hyperedgeRerouter;
    router.delete?.();
  } catch (error) {
    routerHyperedgeRerouter = `error: ${error.message}`;
  }

  const enums = {
    RoutingParameter: Object.keys(Avoid.RoutingParameter ?? {}),
    RoutingOption: Object.keys(Avoid.RoutingOption ?? {}),
    ConnDirFlag: Object.keys(Avoid.ConnDirFlag ?? Avoid.ConnDirFlags ?? {}),
  };
  const hasHyperedge = hyperedgeExports.length > 0 || routerHyperedgeRerouter === "function";

  result.data = { libavoid_js: version, exported, hyperedgeExports, routerHyperedgeRerouter, classes, enums };
  result.ok = hasHyperedge;
  result.summary = [
    `libavoid-js ${version}: ${exported.length} exported names`,
    `hyperedge exports: ${hyperedgeExports.length ? hyperedgeExports.join(", ") : "none"}; router.hyperedgeRerouter is ${routerHyperedgeRerouter}`,
    `JunctionRef methods: ${classes.JunctionRef ? classes.JunctionRef.join(", ") || "(none listed)" : "class not exposed"}`,
    `ConnRef route()/displayRoute(): ${classes.ConnRef ? ["route", "displayRoute"].map((m) => `${m}=${classes.ConnRef.includes(m)}`).join(" ") : "class not exposed"}`,
    `routing options exposed: ${enums.RoutingOption.join(", ") || "none"}`,
  ];
  finish(0);
} catch (error) {
  result.summary = [`could not inspect libavoid-js: ${error.message}`];
  result.data = { error: String(error.stack ?? error) };
  finish(1);
}
