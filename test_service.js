"use strict";

const { spawnSync } = require("node:child_process");

const modules = ["service_contract", "registry_contract"];

let failed = 0;
for (const mod of modules) {
  const result = spawnSync("python3", ["-m", "unittest", "-v", mod], { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    failed += 1;
  }
}
process.exit(failed === 0 ? 0 : 1);
