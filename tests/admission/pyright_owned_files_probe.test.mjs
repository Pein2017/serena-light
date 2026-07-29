import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import test from "node:test";

import {
  EXPECTED_BUNDLE,
  ProbeError,
  assertRequiredBundleModules,
  rewriteBootstrapSource,
  validateBundleEvidence,
} from "./pyright_owned_files_probe.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const PROBE = join(HERE, "pyright_owned_files_probe.mjs");
const PYTHON = "/root/miniconda3/envs/ms/bin/python";
const LIVE_DEFAULT_PROJECTS = [
  "/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers",
  "/data/ms-swift",
];

function makeFixture(t, { broadInclude = false } = {}) {
  const root = mkdtempSync(join(tmpdir(), "serena-light-pyright-owned-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));

  mkdirSync(join(root, "src"), { recursive: true });
  mkdirSync(join(root, "omitted"), { recursive: true });
  mkdirSync(join(root, "ignored-generated"), { recursive: true });
  writeFileSync(join(root, "src", "main.py"), "answer = 42\n");
  writeFileSync(join(root, "src", "helper.py"), "helper = True\n");
  writeFileSync(join(root, "omitted", "trusted.py"), "omitted = True\n");
  writeFileSync(join(root, "ignored-generated", "hidden.py"), "hidden = True\n");
  writeFileSync(join(root, ".gitignore"), "ignored-generated/\n");
  writeFileSync(
    join(root, "pyrightconfig.json"),
    JSON.stringify({ include: broadInclude ? ["**/*.py"] : ["src"] }),
  );
  return root;
}

function runProbe(root) {
  return spawnSync(
    process.execPath,
    ["--", PROBE, "--pythonpath", PYTHON, "-p", root],
    { encoding: "utf8", maxBuffer: 16 * 1024 * 1024, timeout: 10_000 },
  );
}

test("native configured program emits the compatible fixture's sorted absolute owned files", (t) => {
  const root = makeFixture(t);
  const completed = runProbe(root);

  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(completed.stderr, "");
  const report = JSON.parse(completed.stdout);
  assert.deepEqual(report.owned_files, [
    resolve(root, "src/helper.py"),
    resolve(root, "src/main.py"),
  ]);
  assert.equal(report.owned_file_count, report.owned_files.length);
  assert.match(report.owned_files_sha256, /^[0-9a-f]{64}$/);
  assert.equal(report.engine.version, EXPECTED_BUNDLE.version);
  assert.equal(report.bundle.pyright_js.sha256, EXPECTED_BUNDLE.pyrightJsSha256);
  assert.equal(report.bundle.pyright_internal_js.sha256, EXPECTED_BUNDLE.pyrightInternalJsSha256);
  assert.equal(report.attribution, "AnalyzerService.getOwnedFiles after native CLI setOptions");
  assert.deepEqual(report.project, {
    selected_config_path: resolve(root, "pyrightconfig.json"),
    project_kind: "configured",
    attribution: "AnalyzerService.getConfigOptions().configFileSource after native CLI setOptions",
  });
});

test("ignored incompatible fixture is reported as engine-owned without trust classification", (t) => {
  const root = makeFixture(t, { broadInclude: true });
  const completed = runProbe(root);

  assert.equal(completed.status, 0, completed.stderr);
  const report = JSON.parse(completed.stdout);
  assert.deepEqual(report.owned_files, [
    resolve(root, "ignored-generated/hidden.py"),
    resolve(root, "omitted/trusted.py"),
    resolve(root, "src/helper.py"),
    resolve(root, "src/main.py"),
  ]);
  assert.equal("scope_compatible" in report, false);
  assert.equal("trust" in report, false);
});

test("native default projects do not mistake -p directories for config files", () => {
  for (const root of LIVE_DEFAULT_PROJECTS) {
    const completed = runProbe(root);
    assert.equal(completed.status, 0, `${root}: ${completed.stderr}`);
    assert.equal(completed.stderr, "", root);
    const report = JSON.parse(completed.stdout);
    assert.deepEqual(report.project, {
      selected_config_path: null,
      project_kind: "workspace_default",
      attribution: "AnalyzerService.getConfigOptions().configFileSource after native CLI setOptions",
    });
  }
});

test("bundle version and hashes fail closed", () => {
  assert.throws(
    () =>
      validateBundleEvidence({
        packageJsonBytes: Buffer.from('{"version":"1.1.404"}'),
        pyrightJsBytes: Buffer.alloc(0),
        pyrightInternalJsBytes: Buffer.alloc(0),
      }),
    (error) => error instanceof ProbeError && error.code === "PYRIGHT_VERSION_MISMATCH",
  );

  assert.throws(
    () =>
      validateBundleEvidence({
        packageJsonBytes: Buffer.from(`{"version":"${EXPECTED_BUNDLE.version}"}`),
        pyrightJsBytes: Buffer.from("wrong pyright bundle"),
        pyrightInternalJsBytes: Buffer.from("wrong internal bundle"),
      }),
    (error) => error instanceof ProbeError && error.code === "PYRIGHT_JS_HASH_MISMATCH",
  );

  assert.throws(
    () =>
      validateBundleEvidence({
        packageJsonBytes: Buffer.from(`{"version":"${EXPECTED_BUNDLE.version}"}`),
        pyrightJsBytes: Buffer.from("expected pyright bundle"),
        pyrightInternalJsBytes: Buffer.from("wrong internal bundle"),
      }, {
        ...EXPECTED_BUNDLE,
        pyrightJsSha256: "9f18b4a6de40b06049e732e22c3000f588abff375dd6bb3e4eb8f47fd89de113",
      }),
    (error) => error instanceof ProbeError && error.code === "PYRIGHT_INTERNAL_JS_HASH_MISMATCH",
  );
});

test("bootstrap and required module structure drift fail closed", () => {
  assert.throws(
    () => rewriteBootstrapSource("not a pinned Pyright webpack bootstrap"),
    (error) => error instanceof ProbeError && error.code === "PYRIGHT_BOOTSTRAP_STRUCTURE_MISMATCH",
  );

  assert.throws(
    () => assertRequiredBundleModules(() => ({})),
    (error) => error instanceof ProbeError && error.code === "PYRIGHT_MODULE_STRUCTURE_MISMATCH",
  );
});
